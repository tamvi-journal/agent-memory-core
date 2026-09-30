import { orderedObject, type JsonValue, type OrderedObject, type PyFloat, type PyInt } from "./encoding.ts";

export function parseLossless(source: string): JsonValue {
  let offset = 0;
  const fail = (message: string): never => { throw new SyntaxError(`${message} at ${offset}`); };
  const whitespace = () => { while (" \t\n\r".includes(source[offset] ?? "\0")) offset++; };
  const string = (): string => {
    if (source[offset++] !== '"') fail("expected string");
    let result = "";
    while (offset < source.length) {
      const char = source[offset++];
      if (char === '"') {
        for (let index = 0; index < result.length; index++) {
          const unit = result.charCodeAt(index);
          if (unit >= 0xd800 && unit <= 0xdbff) {
            const next = result.charCodeAt(index + 1);
            if (!(next >= 0xdc00 && next <= 0xdfff)) fail("lone surrogate in string");
            index++;
          } else if (unit >= 0xdc00 && unit <= 0xdfff) fail("lone surrogate in string");
        }
        return result;
      }
      if (char !== "\\") {
        if (char.charCodeAt(0) <= 0x1f) fail("unescaped control character");
        result += char;
        continue;
      }
      const escape = source[offset++];
      const short: Record<string, string> = { '"': '"', "\\": "\\", "/": "/", b: "\b", f: "\f", n: "\n", r: "\r", t: "\t" };
      if (escape in short) result += short[escape];
      else if (escape === "u") {
        const hex = source.slice(offset, offset + 4);
        if (!/^[0-9a-fA-F]{4}$/.test(hex)) fail("invalid unicode escape");
        result += String.fromCharCode(Number.parseInt(hex, 16));
        offset += 4;
      } else fail("invalid escape");
    }
    return fail("unterminated string");
  };
  const number = (): PyInt | PyFloat => {
    const match = source.slice(offset).match(/^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/);
    if (!match) fail("invalid number");
    offset += match[0].length;
    if (/[.eE]/.test(match[0])) {
      const parsed = Number(match[0]);
      if (!Number.isFinite(parsed)) fail("non-finite number");
      return { kind: "float", value: parsed, text: match[0] };
    }
    return { kind: "int", value: BigInt(match[0]), text: match[0] };
  };
  const value = (): JsonValue => {
    whitespace();
    const char = source[offset];
    if (char === '"') return string();
    if (char === "[") {
      offset++; const result: JsonValue[] = []; whitespace();
      if (source[offset] === "]") { offset++; return result; }
      while (true) {
        result.push(value()); whitespace();
        if (source[offset] === "]") { offset++; return result; }
        if (source[offset++] !== ",") fail("expected comma");
      }
    }
    if (char === "{") {
      offset++; const entries: [string, JsonValue][] = []; whitespace();
      if (source[offset] === "}") { offset++; return orderedObject(entries); }
      while (true) {
        whitespace(); const key = string(); whitespace();
        if (source[offset++] !== ":") fail("expected colon");
        entries.push([key, value()]); whitespace();
        if (source[offset] === "}") { offset++; return orderedObject(entries); }
        if (source[offset++] !== ",") fail("expected comma");
      }
    }
    if (source.startsWith("true", offset)) { offset += 4; return true; }
    if (source.startsWith("false", offset)) { offset += 5; return false; }
    if (source.startsWith("null", offset)) { offset += 4; return null; }
    return number();
  };
  const result = value(); whitespace();
  if (offset !== source.length) fail("trailing input");
  return result;
}

export function objectEntries(value: JsonValue): [string, JsonValue][] {
  if (!value || Array.isArray(value) || typeof value !== "object" || value.kind !== "object") {
    throw new TypeError("expected ordered object");
  }
  return value.entries;
}

export function get(value: OrderedObject, key: string): JsonValue | undefined {
  return value.entries.find(([name]) => name === key)?.[1];
}

export function asString(value: JsonValue | undefined, fallback = ""): string {
  if (value === undefined) return fallback;
  if (typeof value !== "string") throw new TypeError("expected string");
  return value;
}

export function asArray(value: JsonValue | undefined): JsonValue[] {
  if (!Array.isArray(value)) throw new TypeError("expected array");
  return value;
}

export function asNumber(value: JsonValue | undefined): number {
  if (!value || typeof value !== "object" || !("kind" in value) || !["int", "float"].includes(value.kind)) throw new TypeError("expected number");
  return value.kind === "int" ? Number(value.value) : value.value;
}
