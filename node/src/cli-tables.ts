import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { asArray, asNumber, asString, get, objectEntries, parseLossless } from "./json.ts";
import type { OrderedObject } from "./encoding.ts";
const PINNED: Record<string, string> = {
  isalnum: "403e6dfe113f69a4388d1a4f43c59007b1db03020c5a8997c632d7552aa620a6",
  decimal: "bbe57bda8eefac096ed91f4f978524ca42637cc1972cae9dcc5988527ca9f67a",
  whitespace: "f6672243ac56ca68888b5039dce1cc2e4457fb15b00676c724c7ea902aa4760a",
};
const cache = new Map<string, OrderedObject>();
function table(name: string): OrderedObject {
  if (cache.has(name)) return cache.get(name)!;
  const path = fileURLToPath(new URL(`../../spec/golden-cli-v1/${name}.json`, import.meta.url));
  const raw = readFileSync(path);
  if (createHash("sha256").update(raw).digest("hex") !== PINNED[name])
    throw new Error(`CLI ${name} table sha256 mismatch`);
  const data = parseLossless(raw.toString("utf8")) as OrderedObject;
  objectEntries(data);
  if (asString(get(data, "unicode")) !== "14.0.0" || asString(get(data, "schema")) !== `trajecta.cli-${name}/v1`)
    throw new Error(`CLI ${name} table schema mismatch`);
  cache.set(name, data);
  return data;
}
function find(ranges: number[][], cp: number): number[] | undefined {
  let low = 0,
    high = ranges.length - 1;
  while (low <= high) {
    const mid = (low + high) >>> 1,
      range = ranges[mid];
    if (cp < range[0]) high = mid - 1;
    else if (cp > range[1]) low = mid + 1;
    else return range;
  }
  return undefined;
}
let alnum: number[][] | undefined, decimal: number[][] | undefined, whitespace: Set<number> | undefined;
const rows = (name: string) => asArray(get(table(name), "ranges")).map((row) => asArray(row).map((x) => asNumber(x)));
export function isAlnum(cp: number): boolean {
  alnum ??= rows("isalnum");
  return Boolean(find(alnum, cp));
}
export function parseIntToken(token: string): bigint {
  decimal ??= rows("decimal");
  whitespace ??= new Set(asArray(get(table("whitespace"), "integer")).map((x) => asNumber(x)));
  let chars = Array.from(token);
  while (chars.length && whitespace.has(chars[0].codePointAt(0)!)) chars.shift();
  while (chars.length && whitespace.has(chars.at(-1)!.codePointAt(0)!)) chars.pop();
  const sign = chars[0] === "-" ? -1n : 1n;
  if (chars[0] === "-" || chars[0] === "+") chars.shift();
  let result = 0n,
    previousDigit = false;
  for (const char of chars) {
    if (char === "_") {
      if (!previousDigit) throw new Error("invalid integer token");
      previousDigit = false;
      continue;
    }
    const cp = char.codePointAt(0)!,
      range = find(decimal, cp);
    if (!range) throw new Error("invalid integer token");
    result = result * 10n + BigInt(cp - range[0] + range[2]);
    previousDigit = true;
  }
  if (!previousDigit) throw new Error("invalid integer token");
  return sign * result;
}

/** argparse's negative-number lexer is narrower than int(): no underscores. */
export function argparseNegative(token: string): boolean {
  if (!token.startsWith("-")) return false;
  decimal ??= rows("decimal");
  const parts = token.slice(1).split(".");
  const digits = (text: string) => Array.from(text).every((char) => Boolean(find(decimal!, char.codePointAt(0)!)));
  return parts.length === 1
    ? parts[0].length > 0 && digits(parts[0])
    : parts.length === 2 && parts[1].length > 0 && digits(parts[0]) && digits(parts[1]);
}
