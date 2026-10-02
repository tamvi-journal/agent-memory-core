import { strictUtf8, parseProfileJson, strictProfileUtf8 } from "./cli-decode.ts";
import {
  openSync,
  closeSync,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { basename, extname, join } from "node:path";
import {
  compareCodePoint,
  codePointSlice,
  orderedObject,
  pyJsonDumps,
  pyStrip,
  type JsonValue,
  type OrderedObject,
} from "./encoding.ts";
import { ValueError } from "./errors.ts";
import { validateCore } from "./identity.ts";
import { pythonIndentedJson } from "./authority.ts";
import { asString, get, objectEntries, parseLossless } from "./json.ts";
import { profileFromAst, type IdentityProfile } from "./profile.ts";
import { dataDir, expandUser, profileSearchDirs, safeFsName, type Environment } from "./paths.ts";

const MAX_PROFILE_BYTES = 256 * 1024;
const object = (value: JsonValue | undefined): OrderedObject => {
  objectEntries(value as JsonValue);
  return value as OrderedObject;
};
export { strictUtf8 } from "./cli-decode.ts";
const readJson = (file: string) => object(parseProfileJson(strictUtf8(readFileSync(file))));
const isFile = (path: string) => {
  try {
    return statSync(path).isFile();
  } catch {
    return false;
  }
};
const isDir = (path: string) => {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
};
function coreErrors(data: OrderedObject): string[] {
  let core = get(data, "core");
  if (!core || typeof core !== "object" || Array.isArray(core) || core.kind !== "object") core = orderedObject([]);
  const filled = orderedObject([...core.entries]);
  if (!get(filled, "vho_stack")) filled.entries.push(["vho_stack", orderedObject([])]);
  if (!get(filled, "recognition_signature")) filled.entries.push(["recognition_signature", []]);
  return validateCore(filled);
}
function check(data: OrderedObject, origin: string): OrderedObject {
  if (!pyStrip(asString(get(data, "name")))) throw new ValueError(`${origin}: a profile needs a non-empty 'name'`);
  const errors = coreErrors(data);
  if (errors.length) throw new ValueError(`${origin}: ${errors.join("; ")}`);
  return data;
}
export function lastProfile(env: Environment = process.env): string | null {
  try {
    return pyStrip(strictUtf8(readFileSync(join(dataDir(process.platform, env), ".last-profile")))) || null;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code) return null;
    throw error;
  }
}
export function remember(value: string, env: Environment = process.env): void {
  try {
    const root = dataDir(process.platform, env);
    mkdirSync(root, { recursive: true });
    writeFileSync(join(root, ".last-profile"), value + "\n", "utf8");
  } catch {
    /* Python remembers best-effort only. */
  }
}
function install(data: OrderedObject, env: Environment): string {
  const name = pyStrip(asString(get(data, "name")));
  const target = join(dataDir(process.platform, env), "profiles", safeFsName(name));
  mkdirSync(target, { recursive: true });
  // Path.write_text opens/truncates first; strict encode failure leaves the empty file.
  const fd = openSync(join(target, "profile.json"), "w");
  try {
    writeFileSync(fd, strictProfileUtf8(pythonIndentedJson(data, 2, true) + "\n"));
  } finally {
    closeSync(fd);
  }
  return name;
}
export async function fetchProfile(url: string): Promise<OrderedObject> {
  if (!url.startsWith("http://") && !url.startsWith("https://"))
    throw new ValueError("only http(s) profile URLs are supported");
  const response = await fetch(url, {
    headers: { "User-Agent": "trajecta-identity-memory" },
    signal: AbortSignal.timeout(20000),
  });
  if (!response.ok) throw new Error(`HTTP Error ${response.status}: ${response.statusText}`);
  const reader = response.body!.getReader();
  let size = 0;
  const chunks: Uint8Array[] = [];
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const accepted = value.subarray(0, MAX_PROFILE_BYTES + 1 - size);
      chunks.push(accepted);
      size += accepted.length;
      if (size > MAX_PROFILE_BYTES) throw new ValueError("profile is larger than 256 KiB");
    }
  } finally {
    await reader.cancel();
  }
  return check(object(parseProfileJson(strictUtf8(Buffer.concat(chunks)))), url);
}
export function loadByName(value: string, env: Environment = process.env): IdentityProfile {
  let folder = expandUser(value, env);
  if (!existsSync(join(folder, "profile.json"))) {
    const searched = profileSearchDirs(env).map((path) => join(path, safeFsName(value)));
    const found = searched.find((path) => existsSync(join(path, "profile.json")));
    if (!found) throw new Error(`profile ${pyJsonDumps(value)} not found; looked in: ${searched.join(", ")}`);
    folder = found;
  }
  const path = join(folder, "profile.json"),
    data = readJson(path);
  const errors = coreErrors(data);
  if (errors.length) throw new ValueError(`profile ${basename(folder)}: ${errors.join("; ")}`);
  return profileFromAst(data);
}
export async function resolveProfile(
  spec?: string,
  options: { env?: Environment; rememberChoice?: boolean } = {},
): Promise<IdentityProfile> {
  const env = options.env ?? process.env;
  const chosen = pyStrip(spec ?? "") || pyStrip(env.TRAJECTA_IDENTITY_PROFILE ?? "") || lastProfile(env) || "example";
  let profile: IdentityProfile;
  if (chosen.startsWith("https://") || chosen.startsWith("http://"))
    profile = loadByName(install(await fetchProfile(chosen), env), env);
  else {
    const path = expandUser(chosen, env);
    profile =
      isFile(path) && extname(path).toLowerCase() === ".json"
        ? loadByName(install(check(readJson(path), path), env), env)
        : loadByName(chosen, env);
  }
  if (options.rememberChoice !== false) remember(chosen, env);
  return profile;
}
export function listProfiles(env: Environment = process.env): Record<string, unknown>[] {
  const seen = new Map<string, Record<string, unknown>>();
  for (const folder of profileSearchDirs(env)) {
    if (!isDir(folder)) continue;
    for (const name of readdirSync(folder).sort(compareCodePoint)) {
      const child = join(folder, name),
        manifest = join(child, "profile.json");
      if (name.startsWith("_") || !isFile(manifest)) continue;
      let data: OrderedObject;
      try {
        data = readJson(manifest);
      } catch {
        continue;
      }
      const profile = asString(get(data, "name"), name);
      if (!seen.has(profile)) {
        const core = get(data, "core");
        seen.set(profile, {
          name: profile,
          agent: asString(get(data, "agent"), profile),
          summary: codePointSlice(
            core && typeof core === "object" && !Array.isArray(core) && core.kind === "object"
              ? asString(get(core, "summary"))
              : "",
            160,
          ),
          path: child,
        });
      }
    }
  }
  return [...seen.values()];
}
