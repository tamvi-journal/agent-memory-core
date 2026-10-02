/** Python's public decode errors on the CLI profile boundary; the AST stays lossless. */
import { ValueError } from "./errors.ts";
import { parseLossless } from "./json.ts";
import type { JsonValue } from "./encoding.ts";
export class UnicodeDecodeError extends ValueError {}
export class JSONDecodeError extends ValueError {}
export function strictUtf8(bytes: Uint8Array): string {
  const fail = (start: number, end: number, reason: string): never => {
    const location =
      end === start + 1
        ? `byte 0x${bytes[start].toString(16).padStart(2, "0")} in position ${start}`
        : `bytes in position ${start}-${end - 1}`;
    throw new UnicodeDecodeError(`'utf-8' codec can't decode ${location}: ${reason}`);
  };
  for (let i = 0; i < bytes.length; ) {
    const first = bytes[i];
    if (first < 0x80) {
      i++;
      continue;
    }
    const size =
      first >= 0xc2 && first <= 0xdf ? 2 : first >= 0xe0 && first <= 0xef ? 3 : first >= 0xf0 && first <= 0xf4 ? 4 : 0;
    if (!size) fail(i, i + 1, "invalid start byte");
    for (let j = 1; j < size; j++) {
      if (i + j >= bytes.length) fail(i, i + j, "unexpected end of data");
      const next = bytes[i + j];
      if (
        next < 0x80 ||
        next > 0xbf ||
        (j === 1 &&
          ((first === 0xe0 && next < 0xa0) ||
            (first === 0xed && next >= 0xa0) ||
            (first === 0xf0 && next < 0x90) ||
            (first === 0xf4 && next >= 0x90)))
      )
        fail(i, i + j, "invalid continuation byte");
    }
    i += size;
  }
  return new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes);
}
export function parseProfileJson(source: string, allowLoneSurrogates = false): JsonValue {
  const fail = (message: string, offset: number): never => {
    const prefix = Array.from(source.slice(0, offset)).join(""),
      cp = Array.from(prefix).length;
    const line = prefix.split("\n").length,
      column = Array.from(prefix.slice(prefix.lastIndexOf("\n") + 1)).length + 1;
    throw new JSONDecodeError(`${message}: line ${line} column ${column} (char ${cp})`);
  };
  if (source.startsWith("\ufeff")) fail("Unexpected UTF-8 BOM (decode using utf-8-sig)", 0);
  try {
    return parseLossless(source, { allowLoneSurrogates });
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
    const match = /^(.*) at (\d+)$/u.exec(error.message);
    if (!match) throw error;
    let offset = Number(match[2]),
      message = "Expecting value";
    switch (match[1]) {
      case "expected string":
        message = "Expecting property name enclosed in double quotes";
        offset--;
        break;
      case "expected comma":
        message = "Expecting ',' delimiter";
        offset--;
        break;
      case "expected colon":
        message = "Expecting ':' delimiter";
        offset--;
        break;
      case "trailing input":
        message = "Extra data";
        break;
      case "unescaped control character":
        message = "Invalid control character at";
        offset--;
        break;
      case "invalid escape":
        message = "Invalid \\escape";
        offset -= 2;
        break;
      case "invalid unicode escape":
        message = "Invalid \\uXXXX escape";
        offset--;
        break;
      case "unterminated string": {
        message = "Unterminated string starting at";
        let start = -1;
        for (let i = 0; i < source.length; i++) {
          if (start >= 0 && source[i] === "\\") {
            i++;
            continue;
          }
          if (source[i] === '"') start = start < 0 ? i : -1;
        }
        offset = Math.max(0, start);
        break;
      }
      case "lone surrogate in string":
        throw new ValueError("lone surrogate in profile string");
      case "non-finite number":
        throw new ValueError("non-finite number in profile JSON");
    }
    return fail(message, offset);
  }
}
