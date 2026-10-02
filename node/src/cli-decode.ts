/** Python's public decode errors on the CLI profile boundary; the AST stays lossless. */
import { ValueError } from "./errors.ts";
import { parseLossless } from "./json.ts";
import type { JsonValue } from "./encoding.ts";
export class UnicodeDecodeError extends ValueError {}
export class JSONDecodeError extends ValueError {}
export class UnicodeEncodeError extends ValueError {}
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
/** CPython JSON syntax, including its accepted nonfinite constants and surrogate escapes.
 * Error locations come directly from scanner states, never from remapped SyntaxError text.
 */
export function parseProfileJson(source: string): JsonValue {
  const fail = (message: string, offset: number): never => {
    const prefix = source.slice(0, offset),
      cp = Array.from(prefix).length;
    const line = prefix.split("\n").length;
    const column = Array.from(prefix.slice(prefix.lastIndexOf("\n") + 1)).length + 1;
    throw new JSONDecodeError(`${message}: line ${line} column ${column} (char ${cp})`);
  };
  if (source.startsWith("\ufeff")) fail("Unexpected UTF-8 BOM (decode using utf-8-sig)", 0);
  return parseLossless(source, {
    allowLoneSurrogates: true,
    allowNonFinite: true,
    pythonError: fail,
    parseInteger(text) {
      const digits = text.startsWith("-") ? text.length - 1 : text.length;
      if (digits > 4300)
        throw new ValueError(
          `Exceeds the limit (4300 digits) for integer string conversion: value has ${digits} digits; ` +
            "use sys.set_int_max_str_digits() to increase the limit",
        );
      return { kind: "int", value: BigInt(text), text };
    },
  });
}

/** Strict Python UTF-8 encode used by profile file writes, not CLI display output. */
export function strictProfileUtf8(source: string): Buffer {
  const chars = Array.from(source);
  for (let start = 0; start < chars.length; start++) {
    const invalid = (index: number) => {
      const cp = chars[index]?.codePointAt(0) ?? 0;
      return cp >= 0xd800 && cp <= 0xdfff;
    };
    if (!invalid(start)) continue;
    let end = start + 1;
    while (invalid(end)) end++;
    const location =
      end === start + 1
        ? `character '\\u${chars[start].codePointAt(0)!.toString(16).padStart(4, "0")}' in position ${start}`
        : `characters in position ${start}-${end - 1}`;
    throw new UnicodeEncodeError(`'utf-8' codec can't encode ${location}: surrogates not allowed`);
  }
  return Buffer.from(source, "utf8");
}
