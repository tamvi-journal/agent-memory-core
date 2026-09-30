import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parseLossless } from "./json.ts";
import { asString, get, objectEntries } from "./json.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const TABLES = resolve(HERE, "..", "tables");
const DIGESTS: Record<string, [string, string]> = {
  "text-norm/v2": ["text-norm-v2.json", "d1b7f523d9bbd35968543ef9582837d8cd28d8bb8a1fb1771f001682618c9b69"],
  "identity-v1": ["identity-v1.json", "51dab4bc79928eef80a063130f4638864a0c97391964c81a14ad32dd999749a9"],
};

const tables = new Map<string, Record<string, string>>();

function loadTable(name: string): Record<string, string> {
  const cached = tables.get(name);
  if (cached) return cached;
  const [file, expected] = DIGESTS[name];
  const raw = readFileSync(resolve(TABLES, file));
  const actual = createHash("sha256").update(raw).digest("hex");
  if (actual !== expected) throw new Error(`normalizer table ${file} sha256 mismatch: ${actual}`);
  const payload = parseLossless(raw.toString("utf8"));
  objectEntries(payload);
  if (asString(get(payload, "schema")) !== "trajecta.norm-table/v1" || asString(get(payload, "name")) !== name || asString(get(payload, "unicode")) !== "14.0.0") throw new Error(`normalizer table ${file} has invalid metadata`);
  const map = Object.fromEntries(objectEntries(get(payload, "map")!).map(([key,value]) => [key, asString(value)]));
  tables.set(name, map);
  return map;
}

function normalize(value: string, name: string): string {
  const table = loadTable(name);
  let folded = "";
  for (const char of value) folded += table[char.codePointAt(0)!.toString(16)] ?? " ";
  return [...folded.matchAll(/[a-z0-9_]+/g)].map((match) => match[0]).join(" ");
}

export const normalizeText = (value: string): string => normalize(value, "text-norm/v2");
export const normalizeIdentityV1 = (value: string): string => normalize(value, "identity-v1");
export const tokens = (value: string): string[] => normalizeText(value).split(" ").filter((part) => part.length > 1);

loadTable("text-norm/v2");
loadTable("identity-v1");
