import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export type PyInt = { kind: "int"; value: bigint; text?: string };
export type PyFloat = { kind: "float"; value: number; text?: string };
export type OrderedObject = { kind: "object"; entries: [string, JsonValue][] };
export type JsonValue = null | boolean | string | PyInt | PyFloat | OrderedObject | JsonValue[];

export const pyInt = (value: bigint | number | string): PyInt => ({ kind: "int", value: BigInt(value) });
export const pyFloat = (value: number): PyFloat => ({ kind: "float", value });
export const orderedObject = (entries: [string, JsonValue][]): OrderedObject => ({ kind: "object", entries });

export function compareCodePoint(a: string, b: string): number {
  const left = Array.from(a, (c) => c.codePointAt(0)!);
  const right = Array.from(b, (c) => c.codePointAt(0)!);
  for (let i = 0; i < Math.min(left.length, right.length); i++) {
    if (left[i] !== right[i]) return left[i] < right[i] ? -1 : 1;
  }
  return left.length - right.length;
}

function digitsAndExponent(value: number): { negative: boolean; digits: string; exponent: number } {
  const negative = value < 0 || Object.is(value, -0);
  const raw = Math.abs(value).toString().toLowerCase();
  const [coefficient, exponentText] = raw.split("e");
  const explicit = exponentText === undefined ? 0 : Number(exponentText);
  const [whole, fraction = ""] = coefficient.split(".");
  let digits = whole + fraction;
  let decimal = whole.length + explicit;
  let leading = 0;
  while (leading < digits.length - 1 && digits[leading] === "0") leading++;
  digits = digits.slice(leading);
  decimal -= leading;
  let trailing = digits.length;
  while (trailing > 1 && digits[trailing - 1] === "0") trailing--;
  digits = digits.slice(0, trailing);
  return { negative, digits, exponent: decimal - 1 };
}

export function pyFloatRepr(value: number): string {
  if (!Number.isFinite(value)) throw new TypeError("NaN and Infinity are not canonical JSON values");
  if (value === 0) return Object.is(value, -0) ? "-0.0" : "0.0";
  const { negative, digits, exponent } = digitsAndExponent(value);
  const sign = negative ? "-" : "";
  if (exponent < -4 || exponent >= 16) {
    const fraction = digits.length > 1 ? `.${digits.slice(1)}` : "";
    const exponentSign = exponent < 0 ? "-" : "+";
    return `${sign}${digits[0]}${fraction}e${exponentSign}${String(Math.abs(exponent)).padStart(2, "0")}`;
  }
  const point = exponent + 1;
  let fixed: string;
  if (point <= 0) fixed = `0.${"0".repeat(-point)}${digits}`;
  else if (point >= digits.length) fixed = digits + "0".repeat(point - digits.length);
  else fixed = `${digits.slice(0, point)}.${digits.slice(point)}`;
  if (!fixed.includes(".")) fixed += ".0";
  return sign + fixed;
}

function doubleFraction(value: number): { negative: boolean; numerator: bigint; denominator: bigint } {
  const buffer = new ArrayBuffer(8);
  const view = new DataView(buffer);
  view.setFloat64(0, value, false);
  const bits = view.getBigUint64(0, false);
  const negative = (bits >> 63n) === 1n;
  const exponentBits = Number((bits >> 52n) & 0x7ffn);
  const fraction = bits & ((1n << 52n) - 1n);
  if (exponentBits === 0x7ff) throw new TypeError("py_fixed requires a finite number");
  if (exponentBits === 0 && fraction === 0n) return { negative, numerator: 0n, denominator: 1n };
  const mantissa = exponentBits === 0 ? fraction : (1n << 52n) | fraction;
  const exponent = exponentBits === 0 ? -1074 : exponentBits - 1023 - 52;
  return exponent >= 0
    ? { negative, numerator: mantissa << BigInt(exponent), denominator: 1n }
    : { negative, numerator: mantissa, denominator: 1n << BigInt(-exponent) };
}

export function pyFixed(value: number, places: number): string {
  if (!Number.isInteger(places) || places < 0) throw new RangeError("places must be a non-negative integer");
  const { negative, numerator, denominator } = doubleFraction(value);
  const scale = 10n ** BigInt(places);
  const scaled = numerator * scale;
  let rounded = scaled / denominator;
  const remainder = scaled % denominator;
  const twice = remainder * 2n;
  if (twice > denominator || (twice === denominator && rounded % 2n === 1n)) rounded++;
  const raw = rounded.toString().padStart(places + 1, "0");
  const body = places === 0 ? raw : `${raw.slice(0, -places)}.${raw.slice(-places)}`;
  return (negative ? "-" : "") + body;
}

export function pyRound(value: number, places: number): number {
  return Number(pyFixed(value, places));
}

function rejectSurrogates(value: string): void {
  for (let i = 0; i < value.length; i++) {
    const unit = value.charCodeAt(i);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const next = value.charCodeAt(i + 1);
      if (!(next >= 0xdc00 && next <= 0xdfff)) throw new TypeError("lone surrogate in string");
      i++;
    } else if (unit >= 0xdc00 && unit <= 0xdfff) throw new TypeError("lone surrogate in string");
  }
}

function quote(value: string): string {
  rejectSurrogates(value);
  let result = '"';
  for (const char of value) {
    const cp = char.codePointAt(0)!;
    if (char === '"') result += '\\"';
    else if (char === "\\") result += "\\\\";
    else if (char === "\b") result += "\\b";
    else if (char === "\f") result += "\\f";
    else if (char === "\n") result += "\\n";
    else if (char === "\r") result += "\\r";
    else if (char === "\t") result += "\\t";
    else if (cp <= 0x1f) result += `\\u${cp.toString(16).padStart(4, "0")}`;
    else result += char;
  }
  return result + '"';
}

export function canonicalJson(value: JsonValue, sortKeys = true): string {
  if (value === null) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "string") return quote(value);
  if (Array.isArray(value)) return `[${value.map((item) => canonicalJson(item, sortKeys)).join(",")}]`;
  if (value.kind === "int") return value.value.toString();
  if (value.kind === "float") return pyFloatRepr(value.value);
  const entries = sortKeys ? [...value.entries].sort(([a], [b]) => compareCodePoint(a, b)) : value.entries;
  return `{${entries.map(([key, item]) => `${quote(key)}:${canonicalJson(item, sortKeys)}`).join(",")}}`;
}

export function hashPayload(value: JsonValue): string {
  return createHash("sha256").update(canonicalJson(value), "utf8").digest("hex");
}

export function floatHex(value: number): string {
  if (!Number.isFinite(value)) return value.toString();
  if (value === 0) return Object.is(value, -0) ? "-0x0.0p+0" : "0x0.0p+0";
  const { negative, numerator, denominator } = doubleFraction(value);
  let exponent = 0;
  let n = numerator;
  let d = denominator;
  while (n >= d * 2n) { d *= 2n; exponent++; }
  while (n < d) { n *= 2n; exponent--; }
  const fraction = n - d;
  const hex = ((fraction << 52n) / d).toString(16).padStart(13, "0");
  return `${negative ? "-" : ""}0x1.${hex}p${exponent >= 0 ? "+" : ""}${exponent}`;
}

export function codePointSlice(value: string, end: number): string {
  return Array.from(value).slice(0, end).join("");
}

const PY_WHITESPACE = /[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+/u;
const PY_EDGE_WHITESPACE = /^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/gu;

export function pySplit(value: string): string[] {
  return value.split(PY_WHITESPACE).filter((part) => part.length > 0);
}

export function pyStrip(value: string): string {
  return value.replace(PY_EDGE_WHITESPACE, "");
}

/** Python `datetime.now(timezone.utc).isoformat(timespec="seconds")`. */
export function utcNowSeconds(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/u, "+00:00");
}

const TITLE_TABLE = "py-title-u14.json";
const TITLE_SHA256 = "1ce9996ab9e3dad9cda2cee963f66c7fe23f0c18d14420b6a3e5aa0fb6a0031a";
let titleTable: { cased: [number, number][]; title: Record<string, string>; lower: Record<string, string> } | null = null;

function loadTitleTable() {
  if (titleTable) return titleTable;
  const raw = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), "..", "tables", TITLE_TABLE));
  const actual = createHash("sha256").update(raw).digest("hex");
  if (actual !== TITLE_SHA256) throw new Error(`title table ${TITLE_TABLE} sha256 mismatch: ${actual}`);
  const payload = JSON.parse(raw.toString("utf8"));
  if (payload.schema !== "trajecta.py-title-table/v1" || payload.unicode !== "14.0.0") {
    throw new Error(`title table ${TITLE_TABLE} has invalid metadata`);
  }
  titleTable = { cased: payload.cased, title: payload.title, lower: payload.lower };
  return titleTable;
}

function isCased(cp: number): boolean {
  const ranges = loadTitleTable().cased;
  let low = 0;
  let high = ranges.length - 1;
  while (low <= high) {
    const middle = (low + high) >> 1;
    const [first, last] = ranges[middle];
    if (cp < first) high = middle - 1;
    else if (cp > last) low = middle + 1;
    else return true;
  }
  return false;
}

/**
 * Python 3.11 `str.title()` (Unicode 14), from a table frozen from the
 * oracle (tools/title_table/generate.py): after a Cased character, the full
 * lowercase mapping; otherwise the full titlecase mapping. "Cased" is the
 * derived property (includes Other_Lowercase such as U+00AA and U+24B6..).
 */
export function pyTitle(value: string): string {
  const table = loadTitleTable();
  let previousCased = false;
  let result = "";
  for (const char of value) {
    const cp = char.codePointAt(0)!;
    const key = cp.toString(16);
    result += previousCased ? (table.lower[key] ?? char) : (table.title[key] ?? char);
    previousCased = isCased(cp);
  }
  return result;
}
