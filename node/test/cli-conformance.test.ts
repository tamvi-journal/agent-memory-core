import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { createServer } from "node:http";
import {
  copyFileSync,
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  realpathSync,
  rmSync,
  statSync,
  utimesSync,
} from "node:fs";
import { dirname, resolve, relative, isAbsolute } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { DatabaseSync } from "node:sqlite";
import {
  asArray,
  asString,
  compareCodePoint,
  floatHex,
  get,
  parseLossless,
  pyFloatRepr,
  type JsonValue,
  type OrderedObject,
} from "../src/index.ts";
import { render, type Token } from "./cli/tokens.ts";
const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../.."),
  CORPUS = resolve(ROOT, "spec/golden-cli-v1");
const sha = (bytes: Buffer) => createHash("sha256").update(bytes).digest("hex");
function plain(value: JsonValue): any {
  if (value === null || typeof value !== "object") return value;
  if (Array.isArray(value)) return value.map(plain);
  if (value.kind === "int") return Number(value.value);
  if (value.kind === "float") return value.value;
  return Object.fromEntries(value.entries.map(([key, item]) => [key, plain(item)]));
}
const metadata = (path: string): any => plain(parseLossless(readFileSync(path, "utf8")));
function inventory(root: string): string[] {
  const result: string[] = [];
  for (const name of readdirSync(root).sort(compareCodePoint)) {
    const path = resolve(root, name);
    result.push(relative(root, path).replaceAll("\\", "/"));
    if (statSync(path).isDirectory()) result.push(...inventory(path).map((child) => name + "/" + child));
  }
  return result.sort(compareCodePoint);
}
function contain(root: string, path: string): void {
  const part = relative(root, resolve(path));
  assert(
    !isAbsolute(part) && part !== ".." && !part.startsWith("..\\") && !part.startsWith("../"),
    `outside fixture: ${path}`,
  );
}
export function dumpDatabase(path: string): unknown {
  const db = new DatabaseSync(path, { readOnly: true });
  try {
    const tables: Record<string, unknown[]> = {};
    for (const { name } of db.prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").all() as {
      name: string;
    }[]) {
      const quote = (s: string) => `"${s.replaceAll('"', '""')}"`,
        columns = db.prepare(`PRAGMA table_info(${quote(name)})`).all() as any[];
      const pk = columns
        .filter((c) => c.pk)
        .sort((a, b) => a.pk - b.pk)
        .map((c) => quote(c.name));
      const floats = new Set(columns.filter((c) => String(c.type).toUpperCase().includes("REAL")).map((c) => c.name));
      tables[name] = (
        db.prepare(`SELECT * FROM ${quote(name)} ORDER BY ${pk.length ? pk.join(",") : "rowid"}`).all() as Record<
          string,
          unknown
        >[]
      ).map((row) =>
        Object.fromEntries(
          Object.entries(row).map(([key, value]) => [
            key,
            typeof value === "number" && floats.has(key) ? { repr: pyFloatRepr(value), hex: floatHex(value) } : value,
          ]),
        ),
      );
    }
    return { tables };
  } finally {
    db.close();
  }
}
function schemaRecord(path: string): unknown {
  const db = new DatabaseSync(path, { readOnly: true });
  try {
    return {
      application_id: db.prepare("PRAGMA application_id").get()!.application_id,
      user_version: db.prepare("PRAGMA user_version").get()!.user_version,
      sqlite_master: db
        .prepare("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")
        .all()
        .map((row) => [row.type, row.name, row.tbl_name, row.sql]),
    };
  } finally {
    db.close();
  }
}
function fileState(path: string): unknown {
  const stat = statSync(path, { bigint: true });
  return { mtime: stat.mtimeNs, mode: stat.mode & 0o7777n, sha256: sha(readFileSync(path)) };
}
let authenticated = false;
function verifyManifest(): void {
  const manifest = metadata(resolve(CORPUS, "MANIFEST.json"));
  assert.equal(manifest.schema, "trajecta.golden-cli-manifest/v1");
  assert.deepEqual(
    inventory(CORPUS).filter((path) => statSync(resolve(CORPUS, path)).isFile() && path !== "MANIFEST.json"),
    Object.keys(manifest.files).sort(compareCodePoint),
  );
  for (const [path, digest] of Object.entries(manifest.files))
    assert.equal(sha(readFileSync(resolve(CORPUS, path))), digest, path);
  authenticated = true;
}
test("CLI MANIFEST authenticates every fixture, template, registry and table", verifyManifest);
for (const name of readdirSync(CORPUS)
  .filter((name) => statSync(resolve(CORPUS, name)).isDirectory())
  .sort(compareCodePoint)) {
  test(`CLI corpus ${name}`, async (t) => {
    if (!authenticated) verifyManifest();
    const scenario = resolve(CORPUS, name),
      expected = metadata(resolve(scenario, "expected.json"));
    const root = realpathSync.native(mkdtempSync(resolve(ROOT, ".cli-replay-")));
    t.after(() => rmSync(root, { recursive: true, force: true }));
    cpSync(resolve(scenario, "fixture"), root, { recursive: true });
    const invocation = metadata(resolve(scenario, "invocation.json"));
    for (const [path, seconds] of Object.entries(invocation.file_times ?? {}) as [string, number][]) {
      contain(root, resolve(root, path));
      utimesSync(resolve(root, path), seconds, seconds);
    }
    const body = existsSync(resolve(root, "recipe.json"))
      ? readFileSync(resolve(root, "recipe.json"))
      : Buffer.from("{}");
    const server = createServer((req, res) => {
      assert.equal(req.headers["user-agent"], "trajecta-identity-memory");
      res.end(body);
    });
    await new Promise<void>((done) => server.listen(0, "127.0.0.1", done));
    t.after(() => new Promise<void>((done) => server.close(() => done())));
    const address = server.address();
    assert(address && typeof address === "object");
    const origin = `http://127.0.0.1:${address.port}`,
      registry: Token[] = metadata(resolve(scenario, "tokens.json"));
    const argv = asArray(
      parseLossless(
        render(readFileSync(resolve(scenario, "argv.json")), registry, "argv.json", root, origin).toString("utf8"),
      ),
    ).map(asString);
    const env: Record<string, string> = { PATH: process.env.PATH ?? "" };
    if (process.platform === "win32")
      for (const key of ["SYSTEMROOT", "COMSPEC"]) if (process.env[key]) env[key] = process.env[key]!;
    const envValues = parseLossless(
      render(readFileSync(resolve(scenario, "env.json")), registry, "env.json", root, origin).toString("utf8"),
    ) as OrderedObject;
    for (const [key, value] of envValues.entries) env[key] = asString(value);
    for (const key of [
      "HOME",
      "USERPROFILE",
      "XDG_DATA_HOME",
      "LOCALAPPDATA",
      "APPDATA",
      "TRAJECTA_IDENTITY_DATA_DIR",
      "TRAJECTA_IDENTITY_PROFILES",
    ])
      contain(root, env[key]);
    if (env.TRAJECTA_WORK_ROOT) contain(root, env.TRAJECTA_WORK_ROOT);
    for (let i = 0; i < argv.length; i++)
      if (["--db", "--backup", "--out", "migrate-to"].includes(argv[i])) contain(root, resolve(root, argv[i + 1]));
    const before = new Map<string, unknown>();
    for (const [path, value] of Object.entries(expected.databases) as [string, any][]) {
      if (value.class === "unchanged") {
        before.set(path, fileState(resolve(root, path)));
        for (const suffix of ["-wal", "-shm", "-journal"])
          if (existsSync(resolve(root, path + suffix)))
            before.set(path + suffix, fileState(resolve(root, path + suffix)));
      }
    }
    const child = spawn(
      process.execPath,
      [
        "--experimental-strip-types",
        resolve(ROOT, "node/test/cli/child.ts"),
        resolve(scenario, "invocation.json"),
        ...argv,
      ],
      { cwd: root, env, stdio: ["pipe", "pipe", "pipe"] },
    );
    const stdout: Buffer[] = [],
      stderr: Buffer[] = [];
    child.stdout.on("data", (bytes) => stdout.push(Buffer.from(bytes)));
    child.stderr.on("data", (bytes) => stderr.push(Buffer.from(bytes)));
    child.stdin.end(readFileSync(resolve(scenario, "stdin")));
    const code = await new Promise<number | null>((done, reject) => {
      child.on("close", done);
      child.on("error", reject);
    });
    for (const [path, state] of before)
      assert.deepEqual(fileState(resolve(root, path)), state, `unchanged stat/bytes ${path}`);
    for (const [path, value] of Object.entries(expected.databases) as [string, any][]) {
      const full = resolve(root, path);
      if (value.class === "absent") assert(!existsSync(full), `absent ${path}`);
      else if (value.class === "backup") {
        assert.deepEqual(
          readFileSync(full),
          readFileSync(resolve(scenario, "fixture", value.source)),
          `backup ${path}`,
        );
        assert.equal(
          statSync(full, { bigint: true }).mtimeNs,
          statSync(resolve(root, value.source), { bigint: true }).mtimeNs,
          `backup mtime ${path}`,
        );
      } else if (value.class === "written") {
        for (const suffix of ["-wal", "-shm", "-journal"])
          assert(!existsSync(full + suffix), `written sidecar ${path}${suffix}`);
        assert.deepEqual(schemaRecord(full), value.schema, `schema ${path}`);
      } else assert.equal(value.class, "unchanged", `unknown oracle class ${path}`);
    }
    assert.equal(code, expected.exit, Buffer.concat(stderr).toString());
    let actualOut = Buffer.concat(stdout),
      actualErr = Buffer.concat(stderr);
    let expectedOut = render(readFileSync(resolve(scenario, "stdout")), registry, "stdout", root, origin);
    const expectedErr = render(readFileSync(resolve(scenario, "stderr")), registry, "stderr", root, origin);
    if (argv.includes("setup") && code === 0) {
      const actual = parseLossless(actualOut.toString("utf8")) as OrderedObject;
      const expectedSetup = parseLossless(expectedOut.toString("utf8")) as OrderedObject;
      const aServers = get(actual, "mcpServers") as OrderedObject,
        eServers = get(expectedSetup, "mcpServers") as OrderedObject;
      assert.deepEqual(
        aServers.entries.map(([key]) => key),
        eServers.entries.map(([key]) => key),
      );
      for (let i = 0; i < aServers.entries.length; i++) {
        const a = aServers.entries[i][1] as OrderedObject,
          e = eServers.entries[i][1] as OrderedObject;
        assert.deepEqual(
          a.entries.map(([key]) => key),
          e.entries.map(([key]) => key),
        );
        assert.equal(get(a, "command"), process.execPath);
        const aa = asArray(get(a, "args")).map(asString),
          ea = asArray(get(e, "args")).map(asString);
        assert.deepEqual(aa.slice(0, 2), ["--experimental-strip-types", resolve(ROOT, "node/src/mcp.ts")]);
        assert.deepEqual(ea.slice(0, 2), ["-m", "trajecta_identity.mcp_server"]);
        assert.deepEqual(aa.slice(2), ea.slice(2));
      }
    } else if (expected.comparison !== "help") assert.deepEqual(actualOut, expectedOut, "stdout bytes");
    if (expected.comparison === "usage") {
      assert(actualErr.toString("utf8").trim().split(/\r?\n/u).at(-1)!.startsWith("trajecta-identity: error:"));
    } else if (expected.comparison === "crash") assert(actualErr.length > 0);
    else assert.deepEqual(actualErr, expectedErr, "stderr bytes");
    assert.deepEqual(inventory(root), Object.keys(expected.tree).sort(compareCodePoint), "entire post-run tree");
    for (const [path, value] of Object.entries(expected.tree) as [string, any][]) {
      const full = resolve(root, path);
      if (value.kind === "directory") {
        assert(statSync(full).isDirectory());
        continue;
      }
      if (value.kind === "store") {
        // R0 dump parity is the SQLite comparison law (§3.7); refusal bytes are also checked below.
        const copy = resolve(root, ".dump-copy");
        copyFileSync(full, copy);
        try {
          assert.deepEqual(dumpDatabase(copy), expected.dumps[value.dump], `dump ${path}`);
        } finally {
          rmSync(copy);
        }
        if (value.unchanged_sha256) assert.equal(sha(readFileSync(full)), value.unchanged_sha256, path);
      } else if (value.template)
        assert.equal(
          sha(readFileSync(full)),
          sha(render(readFileSync(resolve(scenario, value.template)), registry, "data/.last-profile", root, origin)),
          path,
        );
      else assert.equal(sha(readFileSync(full)), value.sha256, path);
    }
  });
}
