import { orderedObject, type JsonValue, type OrderedObject, type PyFloat, type PyInt } from "./encoding.ts";

export function parseLossless(
  source: string,
  options: {
    allowLoneSurrogates?: boolean;
    allowNonFinite?: boolean;
    parseInteger?: (text: string) => PyInt;
    pythonError?: (message: string, offset: number) => never;
  } = {},
): JsonValue {
  let offset = 0;
  const fail = (message: string, pythonMessage = "Expecting value", at = offset): never => {
    if (options.pythonError) return options.pythonError(pythonMessage, at);
    throw new SyntaxError(`${message} at ${offset}`);
  };
  const whitespace = () => {
    while (" \t\n\r".includes(source[offset] ?? "\0")) offset++;
  };
  const string = (): string => {
    const start = offset;
    if (source[offset++] !== '"') fail("expected string", "Expecting property name enclosed in double quotes", start);
    let result = "";
    while (offset < source.length) {
      const char = source[offset++];
      if (char === '"') {
        if (!options.allowLoneSurrogates)
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
        if (char.charCodeAt(0) <= 0x1f) fail("unescaped control character", "Invalid control character at", offset - 1);
        result += char;
        continue;
      }
      if (offset === source.length && options.pythonError)
        fail("unterminated string", "Unterminated string starting at", start);
      const escape = source[offset++];
      const short: Record<string, string> = {
        '"': '"',
        "\\": "\\",
        "/": "/",
        b: "\b",
        f: "\f",
        n: "\n",
        r: "\r",
        t: "\t",
      };
      if (escape in short) result += short[escape];
      else if (escape === "u") {
        const hex = source.slice(offset, offset + 4);
        if (!/^[0-9a-fA-F]{4}$/.test(hex)) fail("invalid unicode escape", "Invalid \\uXXXX escape", offset - 1);
        result += String.fromCharCode(Number.parseInt(hex, 16));
        offset += 4;
      } else fail("invalid escape", "Invalid \\escape", offset - 2);
    }
    return fail("unterminated string", "Unterminated string starting at", start);
  };
  const number = (): PyInt | PyFloat => {
    const match = source.slice(offset).match(/^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/);
    if (!match) fail("invalid number");
    offset += match[0].length;
    if (/[.eE]/.test(match[0])) {
      const parsed = Number(match[0]);
      if (!Number.isFinite(parsed) && !options.allowNonFinite) fail("non-finite number");
      return { kind: "float", value: parsed, text: match[0] };
    }
    return options.parseInteger?.(match[0]) ?? { kind: "int", value: BigInt(match[0]), text: match[0] };
  };
  const value = (): JsonValue => {
    whitespace();
    const char = source[offset];
    if (char === '"') return string();
    if (char === "[") {
      offset++;
      const result: JsonValue[] = [];
      whitespace();
      if (source[offset] === "]") {
        offset++;
        return result;
      }
      while (true) {
        result.push(value());
        whitespace();
        if (source[offset] === "]") {
          offset++;
          return result;
        }
        if (source[offset++] !== ",") fail("expected comma", "Expecting ',' delimiter", offset - 1);
      }
    }
    if (char === "{") {
      offset++;
      const entries: [string, JsonValue][] = [];
      whitespace();
      if (source[offset] === "}") {
        offset++;
        return orderedObject(entries);
      }
      while (true) {
        whitespace();
        const key = string();
        whitespace();
        if (source[offset++] !== ":") fail("expected colon", "Expecting ':' delimiter", offset - 1);
        // Python json: a repeated key keeps its first position and takes the last value.
        const item = value();
        const existing = entries.findIndex(([name]) => name === key);
        if (existing >= 0) entries[existing] = [key, item];
        else entries.push([key, item]);
        whitespace();
        if (source[offset] === "}") {
          offset++;
          return orderedObject(entries);
        }
        if (source[offset++] !== ",") fail("expected comma", "Expecting ',' delimiter", offset - 1);
      }
    }
    if (source.startsWith("true", offset)) {
      offset += 4;
      return true;
    }
    if (source.startsWith("false", offset)) {
      offset += 5;
      return false;
    }
    if (source.startsWith("null", offset)) {
      offset += 4;
      return null;
    }
    if (options.allowNonFinite)
      for (const [text, constant] of [
        ["NaN", NaN],
        ["Infinity", Infinity],
        ["-Infinity", -Infinity],
      ] as const)
        if (source.startsWith(text, offset)) {
          offset += text.length;
          return { kind: "float", value: constant, text };
        }
    return number();
  };
  const result = value();
  whitespace();
  if (offset !== source.length) fail("trailing input", "Extra data");
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
  if (!value || typeof value !== "object" || !("kind" in value) || !["int", "float"].includes(value.kind))
    throw new TypeError("expected number");
  return value.kind === "int" ? Number(value.value) : value.value;
}
