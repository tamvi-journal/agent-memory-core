import assert from "node:assert/strict";
export type Token = { file: string; byte_offset: number; token: string; context: "json-string" | "raw-text" };
const ALLOWED = new Set(["argv.json", "env.json", "stdout", "stderr", "data/.last-profile"]);
export function render(
  raw: Buffer,
  registry: Token[],
  file: string,
  root: string,
  origin: string,
  separator = process.platform === "win32" ? "\\" : "/",
): Buffer {
  const entries = registry.filter((x) => x.file === file).sort((a, b) => a.byte_offset - b.byte_offset);
  const inventory = [...raw.toString("utf8").matchAll(/\{\{(?:ROOT|ORIGIN)\}\}/gu)].map((match) => ({
    offset: Buffer.byteLength(raw.toString("utf8").slice(0, match.index), "utf8"),
    token: match[0],
  }));
  assert.deepEqual(
    inventory,
    entries.map((x) => ({ offset: x.byte_offset, token: x.token })),
    "exact token registry",
  );
  if (entries.length) assert(ALLOWED.has(file), "token outside named boundary");
  let result = raw;
  for (const entry of [...entries].reverse()) {
    assert(["raw-text", "json-string"].includes(entry.context), "explicit token context");
    let value = entry.token === "{{ROOT}}" ? root : origin;
    assert(value, "run token has a value");
    const at = entry.byte_offset;
    let end = at + Buffer.byteLength(entry.token);
    const tail = result.subarray(end);
    assert(!tail.length || [47, 34, 10, 13].includes(tail[0]), "token boundary");
    assert(result.subarray(at, end).equals(Buffer.from(entry.token)));
    if (entry.token === "{{ROOT}}" && result[end] === 47) {
      const suffix = result
        .subarray(end)
        .toString("utf8")
        .split(/["\r\n]/u)[0];
      value += suffix.replaceAll("/", separator);
      end += Buffer.byteLength(suffix);
    }
    if (entry.context === "json-string") value = JSON.stringify(value).slice(1, -1);
    result = Buffer.concat([result.subarray(0, at), Buffer.from(value, "utf8"), result.subarray(end)]);
  }
  return result;
}
