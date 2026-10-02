import assert from "node:assert/strict";
import { cpSync, existsSync, mkdirSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parseProfileJson, strictUtf8 } from "../../src/cli-decode.ts";
import { objectEntries } from "../../src/json.ts";
import { dataDir, profileDb, resolvedPath, safeFsName, type Environment } from "../../src/paths.ts";

const BUNDLED = resolve(dirname(fileURLToPath(import.meta.url)), "../../../trajecta_identity/profiles");
export function isolatedMcpEnv(root: string, extra: Record<string, string> = {}): Record<string, string> {
  const env: Record<string, string> = { PATH: process.env.PATH ?? "" };
  if (process.platform === "win32")
    for (const key of ["SYSTEMROOT", "COMSPEC"]) if (process.env[key]) env[key] = process.env[key]!;
  for (const [key, value] of Object.entries(extra)) {
    assert(["TRAJECTA_WORK_ROOT", "TRAJECTA_IDENTITY_MCP_CLOCK_START"].includes(key), `unapproved child env: ${key}`);
    env[key] = value;
  }
  for (const [key, directory] of Object.entries({
    HOME: "home",
    USERPROFILE: "home",
    XDG_DATA_HOME: "xdg",
    LOCALAPPDATA: "local",
    APPDATA: "app",
    TRAJECTA_IDENTITY_DATA_DIR: "data",
    TRAJECTA_IDENTITY_PROFILES: "profiles",
    TMPDIR: "tmp",
    TMP: "tmp",
    TEMP: "tmp",
  })) {
    env[key] = resolve(root, directory);
    assertContained(root, env[key], env);
    mkdirSync(env[key], { recursive: true });
  }
  cpSync(BUNDLED, env.TRAJECTA_IDENTITY_PROFILES, { recursive: true });
  return env;
}

export function assertContained(root: string, path: string, env: Environment): void {
  const target = resolvedPath(isAbsolute(path) ? path : resolve(root, path), env);
  const part = relative(resolvedPath(root, env), target);
  assert(
    !isAbsolute(part) && part !== ".." && !part.startsWith("../") && !part.startsWith("..\\"),
    "writable path escapes MCP fixture",
  );
}

/** Check all possible startup writes before spawning, including unused default paths. */
export function assertMcpPaths(root: string, env: Environment, profileName: string, database?: string): void {
  if (database !== undefined) assertContained(root, database, env);
  assertContained(root, profileDb(profileName, env), env);
  assertContained(root, resolve(dataDir(process.platform, env), "profiles", safeFsName(profileName)), env);
  assertContained(
    root,
    resolve(dataDir(process.platform, env), "profiles", safeFsName(profileName), "profile.json"),
    env,
  );
  assertContained(root, resolve(dataDir(process.platform, env), ".last-profile"), env);
  if (env.TRAJECTA_WORK_ROOT?.trim()) assertContained(root, env.TRAJECTA_WORK_ROOT.trim(), env);
}

export function profileNameFor(root: string, input: string, env: Environment): string {
  const path = isAbsolute(input) ? input : resolve(root, input);
  const manifest = input.endsWith(".json") ? path : resolve(path, "profile.json");
  if (existsSync(manifest)) {
    const entries = objectEntries(parseProfileJson(strictUtf8(readFileSync(manifest))));
    const name = entries.findLast(([key]) => key === "name")?.[1];
    assert.equal(typeof name, "string");
    return name as string;
  }
  const name = safeFsName(input);
  for (const folder of [env.TRAJECTA_IDENTITY_PROFILES!, resolve(dataDir(process.platform, env), "profiles")]) {
    const manifest = resolve(folder, name, "profile.json");
    if (existsSync(manifest)) return profileNameFor(root, manifest, env);
  }
  throw new Error("named test profile must be in isolated profiles");
}
