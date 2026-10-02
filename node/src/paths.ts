import { realpathSync, lstatSync, readlinkSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, isAbsolute, basename } from "node:path";
import { fileURLToPath } from "node:url";
import { pyStrip } from "./encoding.ts";
import { isAlnum } from "./cli-tables.ts";

export type Environment = Record<string, string | undefined>;
export function expandUser(path: string, env: Environment = process.env): string {
  if (path === "~" || path.startsWith("~/") || path.startsWith("~\\"))
    return join(env.HOME || env.USERPROFILE || homedir(), path.slice(2));
  return path;
}
export function dataDir(platform = process.platform, env: Environment = process.env): string {
  const override = pyStrip(env.TRAJECTA_IDENTITY_DATA_DIR ?? "");
  if (override) return expandUser(override, env);
  const home = env.HOME || env.USERPROFILE || homedir();
  if (platform === "darwin") return join(home, "Library", "Application Support", "Trajecta Identity Memory");
  if (platform.startsWith("win"))
    return join(env.LOCALAPPDATA || env.APPDATA || join(home, "AppData", "Local"), "Trajecta Identity Memory");
  return join(pyStrip(env.XDG_DATA_HOME ?? "") || join(home, ".local", "share"), "trajecta-identity-memory");
}
const RESERVED = new Set([
  "con",
  "prn",
  "aux",
  "nul",
  ...Array.from({ length: 9 }, (_, i) => `com${i + 1}`),
  ...Array.from({ length: 9 }, (_, i) => `lpt${i + 1}`),
]);
export function safeFsName(name: string): string {
  let value =
    Array.from(name)
      .filter((c) => isAlnum(c.codePointAt(0)!) || "-_.".includes(c))
      .join("")
      .replace(/^[. ]+|[. ]+$/gu, "") || "default";
  const dot = value.indexOf("."),
    first = dot < 0 ? value : value.slice(0, dot);
  // Only ASCII reserved spellings can match; Unicode case folding cannot introduce one.
  if (RESERVED.has(first.toLowerCase())) value = first + "_" + (dot < 0 ? "" : value.slice(dot));
  return value;
}
export function profileDb(profile: string, env: Environment = process.env): string {
  return join(dataDir(process.platform, env), safeFsName(profile) + ".sqlite3");
}
export const bundledProfiles = join(dirname(fileURLToPath(import.meta.url)), "../../trajecta_identity/profiles");
export function profileSearchDirs(env: Environment = process.env): string[] {
  return [
    ...(pyStrip(env.TRAJECTA_IDENTITY_PROFILES ?? "") ? [expandUser(env.TRAJECTA_IDENTITY_PROFILES!, env)] : []),
    join(dataDir(process.platform, env), "profiles"),
    bundledProfiles,
  ];
}

/** pathlib.Path.resolve(strict=False), including an existing symlinked parent. */
export function resolvedPath(path: string, env: Environment = process.env, depth = 0): string {
  if (depth > 40) throw new Error("symlink loop in resolved path");
  path = expandUser(path, env) || ".";
  if (!isAbsolute(path)) path = process.cwd() + "/" + path;
  try {
    return realpathSync.native(path);
  } catch (error) {
    try {
      if (lstatSync(path).isSymbolicLink()) {
        const target = readlinkSync(path);
        return resolvedPath(isAbsolute(target) ? target : join(dirname(path), target), env, depth + 1);
      }
    } catch {}
    const parent = dirname(path);
    if (parent === path) throw error;
    return join(resolvedPath(parent, env, depth + 1), basename(path));
  }
}
