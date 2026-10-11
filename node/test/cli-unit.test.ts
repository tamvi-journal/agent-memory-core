import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { parseProfileJson, strictUtf8 } from "../src/cli-decode.ts";
import { canonicalJson, floatHex, type JsonValue } from "../src/encoding.ts";
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
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { spawn, spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import test from "node:test";
import { parseArgs, CliExit, cliJson, confirmationLine, utf8Replace, errorName } from "../src/cli-runtime.ts";

for (const [label, argv, prog] of [
  ["root unknown command", ["unknown-r3"], "trajecta-identity"],
  ["root leftover argument", ["status", "leftover"], "trajecta-identity"],
  ["command typed option", ["timeline", "--limit", "1.0"], "trajecta-identity timeline"],
  ["command missing positional", ["retrieve"], "trajecta-identity retrieve"],
  ["command exclusive group", ["approve-core", "proposal", "--apply", "--reject"], "trajecta-identity approve-core"],
] as const)
  test(`R3 raising parser: ${label}`, () => {
    assert.throws(
      () => parseArgs([...argv]),
      (error: unknown) => error instanceof CliExit && error.status === 2 && error.message.startsWith(`${prog}: error:`),
    );
  });
import { main } from "../src/cli.ts";
import { parseIntToken, isAlnum } from "../src/cli-tables.ts";
import { dataDir, safeFsName, resolvedPath } from "../src/paths.ts";
import { fetchProfile } from "../src/recipe.ts";
import {
  IdentityMemory,
  MemoryStore,
  InjectedClock,
  ValueError,
  RuntimeError,
  ConfirmationMismatch,
  HumanPresenceRequired,
  ReceiptNotFound,
  ReceiptIntegrityError,
  ProposalIntegrityError,
  ProposalDecided,
  StaleAuthority,
  SchemaVersionError,
  IncompatibleJournalMode,
  MigrationRequired,
  PinnedRecordError,
  FileExistsError,
  WorkStoreError,
  loadProfile,
  asArray,
  asString,
  parseLossless,
} from "../src/index.ts";
import { render, type Token } from "./cli/tokens.ts";
const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../.."),
  CORPUS = resolve(ROOT, "spec/golden-cli-v1");
const LEGACY = resolve(ROOT, "spec/golden/identity-open/store.sqlite3");
function workspace(t: any): string {
  const root = realpathSync.native(mkdtempSync(resolve(ROOT, ".cli-unit-")));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  return root;
}
function envFor(root: string): Record<string, string> {
  const env: Record<string, string> = { PATH: process.env.PATH ?? "" };
  if (process.platform === "win32")
    for (const key of ["SYSTEMROOT", "COMSPEC"]) if (process.env[key]) env[key] = process.env[key]!;
  for (const [key, dir] of Object.entries({
    HOME: "home",
    USERPROFILE: "home",
    XDG_DATA_HOME: "xdg",
    LOCALAPPDATA: "local",
    APPDATA: "app",
    TRAJECTA_IDENTITY_DATA_DIR: "data",
    TRAJECTA_IDENTITY_PROFILES: "profiles",
  }))
    env[key] = resolve(root, dir);
  return env;
}
function fixtureProfile(root: string): void {
  mkdirSync(resolve(root, "profiles/example"), { recursive: true });
  copyFileSync(
    resolve(ROOT, "trajecta_identity/profiles/example/profile.json"),
    resolve(root, "profiles/example/profile.json"),
  );
}
test("CLI UTF-8 replacement occurs only at the output boundary", () => {
  assert.deepEqual(utf8Replace("a\ud800b\udfff😀"), Buffer.from("a?b?😀"));
  assert.equal(cliJson({ text: "\ud800😀" }), '{\n  "text": "?😀"\n}\n');
  assert.equal(cliJson({ confidence: 1.0, revision: 1 }), '{\n  "confidence": 1.0,\n  "revision": 1\n}\n');
});
test("frozen Nd grammar, integer underscores and distinct int whitespace", () => {
  for (const text of ["١٢", "１２", "१२", "1_2", "\u2003+12\u3000"]) assert.equal(parseIntToken(text), 12n);
  assert.equal(parseIntToken("-۱۲"), -12n);
  assert.equal(parseArgs(["timeline", "--limit=-1_2"]).limit, -12n);
  assert.equal(parseArgs(["log-fact", "x", "--title", "-.5", "--summary", "s"]).title, "-.5");
  assert.equal(parseIntToken("9007199254740993"), 9007199254740993n);
  for (const text of ["", "_1", "1_", "1__2", "²", "\x1c1\x1f", "\ufeff1", "1 2", "+_1"])
    assert.throws(() => parseIntToken(text));
});
test("argparse grammar, defaults, abbreviations and no authority bypass", () => {
  assert.throws(() => parseArgs(["--", "status"]));
  assert.equal(parseArgs(["retrieve", "--", "-who"]).cue, "-who");
  assert.equal(parseArgs(["-pexample", "--db=store.sqlite3", "timeline", "--lim=١٢"]).limit, 12n);
  assert.equal(parseArgs(["log-phase", "p", "--title", "t", "--summary", "s"]).content, "");
  for (const argv of [
    ["approve-core", "x", "--apply", "--reject"],
    ["approve-core", "x"],
    ["approve-retract", "x", "--reason", "r", "--yes"],
    ["status", "--db", "late"],
    ["retrieve"],
    ["timeline", "--limit", "-1_2"],
    ["log-fact", "x", "--title", "-x", "--summary", "s"],
    ["import-aml", "vault"],
    ["view"],
    ["plugin"],
  ])
    assert.throws(() => parseArgs(argv));
});
test("safe filesystem names use pinned Unicode 14 truth", () => {
  assert.equal(safeFsName(".. AUX.json .."), "AUX_.json");
  assert.equal(safeFsName("nul"), "nul_");
  assert.equal(safeFsName("  😀  "), "default");
  assert.equal(safeFsName("é１２"), "é１２");
  assert(isAlnum(0x00b2));
  assert(!isAlnum(0x1fae0));
  assert.equal(
    dataDir("linux", { HOME: "/isolated", XDG_DATA_HOME: "/data" }),
    "/data/trajecta-identity-memory".replaceAll("/", process.platform === "win32" ? "\\" : "/"),
  );
});
test("F7 invalid raw UTF-8 is ConfirmationMismatch, with no receipt or operation", async (t) => {
  const root = workspace(t);
  fixtureProfile(root);
  const db = resolve(root, "store.sqlite3");
  const memory = new IdentityMemory(loadProfile(resolve(root, "profiles/example/profile.json")), db, {
    clock: new InjectedClock(),
  });
  memory.bootstrap();
  const proposal = memory.corePropose({
    reason: "CLI guard",
    phaseContext: { kind: "object", entries: [["model", "oracle"]] },
  });
  memory.close();
  const before = readFileSync(db),
    listing = readdirSync(root);
  const terminal = {
    stdinTTY: true,
    stdoutTTY: true,
    write: () => {},
    read: () => confirmationLine(() => Buffer.from([0xff, 10])),
  };
  const stdout: Buffer[] = [],
    stderr: Buffer[] = [];
  const status = await main(["--db", db, "approve-core", String(proposal.proposal_id), "--apply"], {
    env: envFor(root),
    terminal,
    stdout: (b) => stdout.push(b),
    stderr: (b) => stderr.push(b),
    clock: new InjectedClock(),
  });
  assert.equal(status, 1);
  assert.equal(Buffer.concat(stderr).toString(), "ConfirmationMismatch: confirmation input was not valid UTF-8\n");
  assert.deepEqual(readFileSync(db), before);
  assert.deepEqual(
    readdirSync(root).filter((x) => x !== "data"),
    listing,
  );
  assert.throws(
    () =>
      confirmationLine(() => {
        throw new TypeError("internal read bug");
      }),
    /internal read bug/,
  );
});
test("P13 class matching and subclass names; RuntimeError remains a crash", async (t) => {
  const root = workspace(t);
  fixtureProfile(root);
  const env = envFor(root);
  class SpecificValueError extends ValueError {}
  const classes = [
    HumanPresenceRequired,
    ConfirmationMismatch,
    ReceiptNotFound,
    ReceiptIntegrityError,
    ProposalIntegrityError,
    ProposalDecided,
    StaleAuthority,
    SchemaVersionError,
    IncompatibleJournalMode,
    MigrationRequired,
    PinnedRecordError,
    FileExistsError,
    WorkStoreError,
    ValueError,
    SpecificValueError,
  ];
  const original = IdentityMemory.prototype.status;
  t.after(() => {
    IdentityMemory.prototype.status = original;
  });
  for (const Class of classes) {
    const error = new Class("sentinel"),
      out: Buffer[] = [],
      err: Buffer[] = [];
    IdentityMemory.prototype.status = () => {
      throw error;
    };
    assert.equal(await main(["status"], { env, stdout: (b) => out.push(b), stderr: (b) => err.push(b) }), 1);
    assert.equal(Buffer.concat(err).toString(), `${errorName(error)}: sentinel\n`);
  }
  IdentityMemory.prototype.status = () => {
    throw new RuntimeError("internal");
  };
  await assert.rejects(main(["status"], { env }), RuntimeError);
});
for (const alias of ["equal", "symlink", "casefold"])
  test(`P14 ${alias} keeps backup/source intact`, (t) => {
    const root = workspace(t),
      source = resolve(root, "source.sqlite3"),
      target = resolve(root, "out/target.sqlite3");
    copyFileSync(LEGACY, source);
    const bytes = readFileSync(source);
    mkdirSync(resolve(root, "out"));
    let backup = target;
    if (alias === "symlink") {
      if (process.platform === "win32") {
        t.skip("POSIX symlinked-parent proof");
        return;
      }
      symlinkSync(resolve(root, "out"), resolve(root, "alias"), "dir");
      backup = resolve(root, "alias/target.sqlite3");
    }
    if (alias === "casefold") {
      writeFileSync(resolve(root, "caseprobe"), "x");
      if (!existsSync(resolve(root, "CASEPROBE"))) {
        t.skip("filesystem is case-sensitive");
        return;
      }
      backup = resolve(root, "out/TARGET.sqlite3");
    }
    const store = new MemoryStore(source);
    t.after(() => store.close());
    const expected =
      alias === "equal" || (alias === "casefold" && process.platform === "win32") ? ValueError : FileExistsError;
    assert.throws(() => store.migrateTo(target, { backupPath: backup }), expected);
    assert.deepEqual(readFileSync(source), bytes);
    if (expected === FileExistsError) {
      assert.deepEqual(readFileSync(backup), bytes);
      assert.deepEqual(readFileSync(target), bytes);
    } else assert(!existsSync(target));
  });
test("P14 equal normalized path refuses before parent creation; missing source ignores backup", (t) => {
  const root = workspace(t),
    source = resolve(root, "source.sqlite3"),
    target = resolve(root, "new/target.sqlite3");
  copyFileSync(LEGACY, source);
  const store = new MemoryStore(source);
  assert.throws(() => store.migrateTo(target, { backupPath: target }), ValueError);
  assert(!existsSync(resolve(root, "new")));
  store.close();
  const missing = new MemoryStore(resolve(root, "missing")),
    result = missing.migrateTo(target, { backupPath: target }) as MemoryStore;
  assert.equal(result.schemaInfo().state, "ready");
  result.close();
  missing.close();
});
test("setup resolution follows a symlinked existing parent", (t) => {
  if (process.platform === "win32") {
    t.skip("POSIX symlink proof");
    return;
  }
  const root = workspace(t);
  mkdirSync(resolve(root, "actual"));
  symlinkSync(resolve(root, "actual"), resolve(root, "alias"), "dir");
  assert.equal(resolvedPath(resolve(root, "alias/new.sqlite3")), resolve(root, "actual/new.sqlite3"));
});
for (const file of ["stdout", "stderr", "data/.last-profile", "argv.json", "env.json"])
  test(`token registry ${file} refuses undeclared, extra-character and wrong-context boundaries`, () => {
    const raw = Buffer.from('"é {{ROOT}}/store.sqlite3 {{ORIGIN}}/profile.json"');
    const registry: Token[] = [
      { file, byte_offset: Buffer.byteLength('"é '), token: "{{ROOT}}", context: "json-string" },
      { file, byte_offset: raw.indexOf(Buffer.from("{{ORIGIN}}")), token: "{{ORIGIN}}", context: "json-string" },
    ];
    const expected = render(raw, registry, file, "C:\\fixture", "http://127.0.0.1:123", "\\");
    assert(expected.includes(Buffer.from("C:\\\\fixture\\\\store.sqlite3")));
    assert(!expected.equals(Buffer.from('"é C:\\\\fixtureX\\\\store.sqlite3 http://127.0.0.1:1239/profile.json"')));
    assert.throws(() => render(raw, registry.slice(1), file, "/root", "http://127.0.0.1:123"));
    const wrong = Buffer.from("{{ROOT}}X"),
      entry: Token = { file, byte_offset: 0, token: "{{ROOT}}", context: "raw-text" };
    assert.throws(() => render(wrong, [entry], file, "/root", ""));
    assert.throws(() =>
      render(
        raw,
        registry.map((x) => ({ ...x, context: "bogus" as any })),
        file,
        "/root",
        "http://127.0.0.1:123",
      ),
    );
  });
for (const mode of ["core-apply", "retract", "legacy-close"])
  test(`real PTY owner smoke ${mode}`, async (t) => {
    if (process.platform === "win32") {
      t.skip("real PTY smoke is Linux/macOS; injected Terminal covers Windows");
      return;
    }
    const root = workspace(t),
      scenario = resolve(CORPUS, `authority-${mode}-issue-only`);
    const { cpSync } = await import("node:fs");
    cpSync(resolve(scenario, "fixture"), root, { recursive: true });
    const argv = asArray(parseLossless(readFileSync(resolve(scenario, "argv.json"), "utf8"))).map(asString);
    const quote = (s: string) => "'" + s.replaceAll("'", "'\\''") + "'";
    const command = [process.execPath, "--experimental-strip-types", resolve(ROOT, "node/src/cli.ts"), ...argv]
      .map(quote)
      .join(" ");
    const script =
      process.platform === "darwin"
        ? ["-q", "/dev/null", "/bin/sh", "-c", command]
        : ["-q", "-e", "-c", command, "/dev/null"];
    // BSD script rejects libuv's socket-backed stdin. cat supplies a real pipe.
    const launch = "cat | script " + script.map(quote).join(" ");
    const child = spawn("/bin/sh", ["-c", launch], { cwd: root, env: envFor(root), stdio: ["pipe", "pipe", "pipe"] });
    const out: Buffer[] = [],
      err: Buffer[] = [];
    let sent = false;
    child.stdout.on("data", (bytes) => {
      out.push(Buffer.from(bytes));
      if (!sent && Buffer.concat(out).includes(Buffer.from("to issue the owner receipt: "))) {
        sent = true;
        child.stdin.write(readFileSync(resolve(scenario, "stdin")));
      }
    });
    child.stdout.on("data", () => {
      if (Buffer.concat(out).includes(Buffer.from('"receipt_id": "receipt:'))) child.stdin.end();
    });
    child.stderr.on("data", (b) => err.push(Buffer.from(b)));
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
    }, 15000);
    t.after(() => clearTimeout(timer));
    const code = await new Promise<number | null>((done, reject) => {
      child.on("close", done);
      child.on("error", reject);
    });
    assert.equal(code, 0, Buffer.concat(err).toString() + Buffer.concat(out).toString());
    assert(sent);
    assert.match(Buffer.concat(out).toString(), /"receipt_id": "receipt:/u);
  });

for (const table of ["isalnum", "decimal", "whitespace"])
  test(`pinned CLI ${table} table refuses altered bytes before parsing`, (t) => {
    const root = workspace(t);
    cpSync(resolve(ROOT, "node/src"), resolve(root, "node/src"), { recursive: true });
    mkdirSync(resolve(root, "spec/golden-cli-v1"), { recursive: true });
    for (const name of ["isalnum", "decimal", "whitespace"])
      copyFileSync(resolve(CORPUS, `${name}.json`), resolve(root, `spec/golden-cli-v1/${name}.json`));
    const path = resolve(root, `spec/golden-cli-v1/${table}.json`);
    writeFileSync(path, Buffer.concat([readFileSync(path), Buffer.from(" ")]));
    const operation = table === "isalnum" ? "isAlnum(65)" : 'parseIntToken("1")';
    const code = `import {isAlnum,parseIntToken} from ${JSON.stringify(pathToFileURL(resolve(root, "node/src/cli-tables.ts")).href)}; ${operation};`;
    const result = spawnSync(process.execPath, ["--experimental-strip-types", "--input-type=module", "-e", code], {
      cwd: root,
      env: envFor(root),
    });
    assert.equal(result.status, 1);
    assert.match(result.stderr.toString(), new RegExp(`CLI ${table} table sha256 mismatch`));
  });

function decodeMetadata(value: JsonValue): any {
  if (Array.isArray(value)) return value.map(decodeMetadata);
  if (value && typeof value === "object") {
    if (value.kind === "object") return Object.fromEntries(value.entries.map(([k, v]) => [k, decodeMetadata(v)]));
    return Number(value.value);
  }
  return value;
}
function decodeCanonical(value: JsonValue): unknown {
  if (value === null) return { kind: "null" };
  if (typeof value === "boolean") return { kind: "bool", value };
  if (typeof value === "string") return { kind: "string", codepoints: Array.from(value, (c) => c.codePointAt(0)!) };
  if (Array.isArray(value)) return { kind: "array", items: value.map(decodeCanonical) };
  if (value.kind === "int") return { kind: "int", value: value.value.toString() };
  if (value.kind === "float")
    return {
      kind: "float",
      value: Number.isNaN(value.value)
        ? "nan"
        : value.value === Infinity
          ? "inf"
          : value.value === -Infinity
            ? "-inf"
            : floatHex(value.value),
    };
  return { kind: "object", entries: value.entries.map(([k, v]) => [decodeCanonical(k), decodeCanonical(v)]) };
}
test("B1 every Python-generated JSON and UTF-8 decode outcome matches exactly", () => {
  const bytes = readFileSync(resolve(CORPUS, "decode-errors.json"));
  const manifest = decodeMetadata(parseLossless(readFileSync(resolve(CORPUS, "MANIFEST.json"), "utf8")));
  assert.equal(createHash("sha256").update(bytes).digest("hex"), manifest.files["decode-errors.json"]);
  const table = decodeMetadata(parseLossless(bytes.toString("utf8")));
  assert.equal(table.schema, "trajecta.cli-decode-outcomes/v1");
  assert.equal(table.unicode, "14.0.0");
  assert.deepEqual(table.mutation_fuzz, { seed: 220085, count: 2000 });
  assert.equal(table.rows.filter((row: any) => row.id.startsWith("json-eof-")).length, 120);
  assert.equal(table.rows.filter((row: any) => row.id.startsWith("json-fuzz-")).length, 2000);
  for (const row of table.rows) {
    let outcome: unknown;
    try {
      const text = strictUtf8(Buffer.from(row.input_hex, "hex"));
      outcome = { ok: decodeCanonical(row.decoder === "json" ? parseProfileJson(text) : text) };
    } catch (error) {
      if (!(error instanceof Error)) throw error;
      outcome = { error: `${error.constructor.name}: ${error.message}` };
    }
    assert.deepEqual(outcome, row.outcome, row.id);
  }
});
test("B1 profile decoding accepts Python extensions while the R0 hash boundary stays strict", () => {
  for (const text of ["NaN", "Infinity", "-Infinity", '"\\ud800"', '"\\udfff"']) {
    const value = parseProfileJson(text);
    assert.throws(() => canonicalJson(value));
    assert.throws(() => parseLossless(text));
  }
});
