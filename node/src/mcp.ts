import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import {
  orderedObject,
  pyFloat,
  pyFloatRepr,
  pyInt,
  pyStrip,
  type JsonValue,
  type OrderedObject,
  type PyFloat,
  type PyInt,
} from "./encoding.ts";
import { InjectedClock } from "./clock.ts";
import { ValueError } from "./errors.ts";
import { IdentityMemory, type FactInput, type PhaseInput } from "./identity.ts";
import { asArray, asString, get, objectEntries, parseLossless } from "./json.ts";
import { TOOL_BY_NAME, toolsJson, validateArguments } from "./mcp-schema.ts";
import { loadProfile } from "./profile.ts";

const SUPPORTED = ["2025-06-18", "2025-03-26", "2024-11-05"];
const NEVER_BOOTSTRAP = new Set([
  "identity_status",
  "identity_retrieve",
  "identity_timeline",
  "identity_core_proposals",
]);
const PUBLIC_ERRORS = new Set([
  "FileExistsError",
  "IncompatibleJournalMode",
  "MigrationRequired",
  "PinnedRecordError",
  "ProposalDecided",
  "ProposalIntegrityError",
  "ReceiptIntegrityError",
  "ReceiptNotFound",
  "RuntimeError",
  "SchemaVersionError",
  "StaleAuthority",
  "ValueError",
  "WorkStoreError",
]);
const FLOAT_FIELDS = new Set([
  "accessibility",
  "confidence",
  "gain",
  "new_value",
  "old_value",
  "salience",
  "score",
  "self_authored_share",
  "stability",
  "weight",
]);

function object(value: JsonValue | undefined): OrderedObject {
  objectEntries(value as JsonValue);
  return value as OrderedObject;
}

function entry(value: OrderedObject, name: string): JsonValue | undefined {
  return get(value, name);
}

function has(value: OrderedObject, name: string): boolean {
  return value.entries.some(([key]) => key === name);
}

function integer(value: JsonValue | undefined): number {
  if (!value || typeof value !== "object" || Array.isArray(value) || value.kind !== "int")
    throw new TypeError("expected integer");
  return Number(value.value);
}

function boolean(value: JsonValue | undefined): boolean {
  if (typeof value !== "boolean") throw new TypeError("expected boolean");
  return value;
}

function strings(value: JsonValue | undefined): string[] {
  return asArray(value).map((item) => asString(item));
}

function optionalString(value: OrderedObject, name: string): string | undefined {
  const found = entry(value, name);
  return found === undefined ? undefined : asString(found);
}

function optionalStrings(value: OrderedObject, name: string): string[] | undefined {
  const found = entry(value, name);
  return found === undefined ? undefined : strings(found);
}

function optionalBoolean(value: OrderedObject, name: string): boolean | undefined {
  const found = entry(value, name);
  return found === undefined ? undefined : boolean(found);
}

function optionalNumeric(value: OrderedObject, name: string): PyInt | PyFloat | undefined {
  const found = entry(value, name);
  if (found === undefined) return undefined;
  if (!found || typeof found !== "object" || Array.isArray(found) || found.kind === "object")
    throw new TypeError("expected numeric value");
  return found;
}

function phaseInput(argumentsValue: OrderedObject): PhaseInput {
  return {
    title: asString(entry(argumentsValue, "title")),
    summary: asString(entry(argumentsValue, "summary")),
    content: optionalString(argumentsValue, "content"),
    follows: optionalStrings(argumentsValue, "follows"),
    causedBy: optionalStrings(argumentsValue, "caused_by"),
    dependsOn: optionalStrings(argumentsValue, "depends_on"),
    decidedBecause: optionalString(argumentsValue, "decided_because"),
    openLoop: optionalBoolean(argumentsValue, "open_loop"),
    workRefs: optionalStrings(argumentsValue, "work_refs"),
    cues: optionalStrings(argumentsValue, "cues"),
    sourceRef: optionalString(argumentsValue, "source_ref"),
    confidence: optionalNumeric(argumentsValue, "confidence"),
    phaseContext: entry(argumentsValue, "phase_context"),
    occurredAt: optionalString(argumentsValue, "occurred_at"),
  };
}

function factInput(argumentsValue: OrderedObject): FactInput {
  return {
    title: asString(entry(argumentsValue, "title")),
    summary: asString(entry(argumentsValue, "summary")),
    content: optionalString(argumentsValue, "content"),
    causedBy: optionalStrings(argumentsValue, "caused_by"),
    dependsOn: optionalStrings(argumentsValue, "depends_on"),
    cues: optionalStrings(argumentsValue, "cues"),
    sourceRef: optionalString(argumentsValue, "source_ref"),
    confidence: optionalNumeric(argumentsValue, "confidence"),
  };
}

export function outputJson(value: unknown, field = ""): JsonValue {
  if (value === null || typeof value === "boolean" || typeof value === "string") return value;
  if (typeof value === "bigint") return pyInt(value);
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return String(value);
    return FLOAT_FIELDS.has(field) || !Number.isInteger(value) ? pyFloat(value) : pyInt(value);
  }
  if (Array.isArray(value)) return value.map((item) => outputJson(item, field));
  if (typeof value === "object" && value) {
    if ("kind" in value && ["int", "float", "object"].includes(String((value as { kind?: unknown }).kind)))
      return value as JsonValue;
    return orderedObject(
      Object.entries(value)
        .filter(([, item]) => item !== undefined)
        .map(([key, item]) => [key, outputJson(item, key)]),
    );
  }
  return String(value);
}

function wireDumps(value: JsonValue): string {
  if (value === null) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "string") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(wireDumps).join(", ")}]`;
  if (value.kind === "int") return value.text ?? value.value.toString();
  if (value.kind === "float") return pyFloatRepr(value.value);
  return `{${value.entries.map(([key, item]) => `${wireDumps(key)}: ${wireDumps(item)}`).join(", ")}}`;
}

function loneSurrogate(value: JsonValue): boolean {
  if (typeof value === "string") {
    for (let index = 0; index < value.length; index++) {
      const unit = value.charCodeAt(index);
      if (unit >= 0xd800 && unit <= 0xdbff) {
        const next = value.charCodeAt(index + 1);
        if (!(next >= 0xdc00 && next <= 0xdfff)) return true;
        index++;
      } else if (unit >= 0xdc00 && unit <= 0xdfff) return true;
    }
    return false;
  }
  if (Array.isArray(value)) return value.some(loneSurrogate);
  if (value && typeof value === "object" && value.kind === "object")
    return value.entries.some(([key, item]) => loneSurrogate(key) || loneSurrogate(item));
  return false;
}

function validId(value: JsonValue): boolean {
  return (
    value === null ||
    typeof value === "string" ||
    Boolean(value && typeof value === "object" && !Array.isArray(value) && value.kind === "int")
  );
}

function ok(id: JsonValue, result: JsonValue): OrderedObject {
  return orderedObject([
    ["jsonrpc", "2.0"],
    ["id", id],
    ["result", result],
  ]);
}

function rpcError(id: JsonValue, code: number, message: string): OrderedObject {
  return orderedObject([
    ["jsonrpc", "2.0"],
    ["id", id],
    [
      "error",
      orderedObject([
        ["code", pyInt(code)],
        ["message", message],
      ]),
    ],
  ]);
}

function toolError(error: unknown): OrderedObject {
  const candidate = error as { name?: string; message?: string; stack?: string };
  const name = String(candidate?.name ?? "Error");
  let text: string;
  if (PUBLIC_ERRORS.has(name)) {
    const wireName = name === "MigrationRequired" ? "MigrationRequiredError" : name;
    let message = String(candidate.message ?? "");
    text = `${wireName}: ${message}`;
  } else {
    process.stderr.write(`${candidate?.stack ?? String(error)}\n`);
    text = "RuntimeError: internal error";
  }
  return orderedObject([
    [
      "content",
      [
        orderedObject([
          ["type", "text"],
          ["text", text],
        ]),
      ],
    ],
    ["isError", true],
  ]);
}

function success(value: unknown): OrderedObject {
  const structured = outputJson(value);
  const text = wireDumps(structured);
  return orderedObject([
    [
      "content",
      [
        orderedObject([
          ["type", "text"],
          ["text", text],
        ]),
      ],
    ],
    ["structuredContent", parseLossless(text)],
    ["isError", false],
  ]);
}

export class McpServer {
  readonly memory: IdentityMemory;
  readonly version: string;

  constructor(memory: IdentityMemory, version: string) {
    this.memory = memory;
    this.version = version;
  }

  dispatch(name: string, argumentsValue: OrderedObject): unknown {
    if (!NEVER_BOOTSTRAP.has(name) && TOOL_BY_NAME.has(name)) {
      if (this.memory.store.schemaInfo().state === "uninitialized") this.memory.bootstrap();
    }
    switch (name) {
      case "identity_status":
        return this.memory.status();
      case "identity_retrieve":
        return this.memory.retrieve(asString(entry(argumentsValue, "cue")), {
          limit: integer(entry(argumentsValue, "limit")),
          tokenBudget: integer(entry(argumentsValue, "budget")),
          includeHistory:
            entry(argumentsValue, "include_history") === undefined
              ? undefined
              : boolean(entry(argumentsValue, "include_history")),
          track: boolean(entry(argumentsValue, "track")),
        });
      case "identity_log_phase":
        return this.memory.logPhase(asString(entry(argumentsValue, "event_id")), phaseInput(argumentsValue));
      case "identity_log_fact":
        return this.memory.logFact(asString(entry(argumentsValue, "fact_id")), factInput(argumentsValue));
      case "identity_core_propose": {
        const phaseContext = object(entry(argumentsValue, "phase_context"));
        if (!phaseContext.entries.length)
          throw new ValueError("a core proposal needs phase_context (model, harness, policies)");
        return this.memory.corePropose({
          reason: asString(entry(argumentsValue, "reason")),
          phaseContext,
          title: optionalString(argumentsValue, "title"),
          summary: optionalString(argumentsValue, "summary"),
          vhoStack:
            entry(argumentsValue, "vho_stack") === undefined ? undefined : object(entry(argumentsValue, "vho_stack")),
          recognitionSignature:
            entry(argumentsValue, "recognition_signature") === undefined
              ? undefined
              : asArray(entry(argumentsValue, "recognition_signature")),
          falsifier: optionalString(argumentsValue, "falsifier"),
          sourceRef: optionalString(argumentsValue, "source_ref"),
        });
      }
      case "identity_core_proposals":
        return { open_core_proposals: this.memory.openCoreProposals() };
      case "identity_core_apply":
        return this.memory.coreApply(asString(entry(argumentsValue, "receipt_id")));
      case "identity_retract":
        return this.memory.retract(asString(entry(argumentsValue, "receipt_id")));
      case "identity_close_legacy_discussion":
        return this.memory.closeLegacyDiscussion(asString(entry(argumentsValue, "receipt_id")));
      case "identity_close_loop":
        return this.memory.closeLoop(
          asString(entry(argumentsValue, "record_id")),
          asString(entry(argumentsValue, "note")),
        );
      case "identity_timeline":
        return { timeline: this.memory.timeline(integer(entry(argumentsValue, "limit"))) };
      default:
        throw new ValueError(`unknown tool: ${name}`);
    }
  }

  handle(value: JsonValue): OrderedObject | null {
    if (!value || Array.isArray(value) || typeof value !== "object" || value.kind !== "object")
      return rpcError(null, -32600, "Invalid Request");
    if (!has(value, "id")) return null;
    const id = entry(value, "id")!;
    if (!validId(id)) return rpcError(null, -32600, "Invalid Request");
    if (loneSurrogate(value)) return rpcError(loneSurrogate(id) ? null : id, -32600, "Invalid Request");
    if (entry(value, "jsonrpc") !== "2.0" || typeof entry(value, "method") !== "string")
      return rpcError(id, -32600, "Invalid Request");
    if (has(value, "params")) {
      const paramsValue = entry(value, "params");
      if (
        !paramsValue ||
        Array.isArray(paramsValue) ||
        typeof paramsValue !== "object" ||
        paramsValue.kind !== "object"
      )
        return rpcError(id, -32602, "Invalid params");
    }

    const method = entry(value, "method") as string;
    const params = has(value, "params") ? object(entry(value, "params")) : orderedObject([]);
    if (method === "initialize") {
      const requested = entry(params, "protocolVersion");
      if (requested !== undefined && typeof requested !== "string") return rpcError(id, -32602, "Invalid params");
      const protocol = typeof requested === "string" && SUPPORTED.includes(requested) ? requested : SUPPORTED[0];
      return ok(
        id,
        orderedObject([
          ["protocolVersion", protocol],
          ["capabilities", orderedObject([["tools", orderedObject([["listChanged", false]])]])],
          [
            "serverInfo",
            orderedObject([
              ["name", "trajecta-identity-memory"],
              ["version", this.version],
            ]),
          ],
          [
            "instructions",
            `Identity memory for ${this.memory.profile.agent}. Recall with identity_retrieve; ` +
              "log your own phases freely; propose core changes for owner decision at the terminal.",
          ],
        ]),
      );
    }
    if (method === "ping") return ok(id, orderedObject([]));
    if (method === "tools/list") return ok(id, orderedObject([["tools", toolsJson()]]));
    if (method !== "tools/call") return rpcError(id, -32601, "Method not found");
    if (!has(params, "name") || typeof entry(params, "name") !== "string")
      return rpcError(id, -32602, "Invalid params");
    if (has(params, "arguments")) {
      const argumentsValue = entry(params, "arguments");
      if (
        !argumentsValue ||
        Array.isArray(argumentsValue) ||
        typeof argumentsValue !== "object" ||
        argumentsValue.kind !== "object"
      )
        return rpcError(id, -32602, "Invalid params");
    }

    const name = entry(params, "name") as string;
    const rawArguments = has(params, "arguments") ? object(entry(params, "arguments")) : orderedObject([]);
    let argumentsValue: OrderedObject;
    try {
      argumentsValue = validateArguments(name, rawArguments);
    } catch (error) {
      return ok(id, toolError(error));
    }

    this.memory.close();
    try {
      return ok(id, success(this.dispatch(name, argumentsValue)));
    } catch (error) {
      return ok(id, toolError(error));
    } finally {
      this.memory.close();
    }
  }
}

export function processFrame(server: McpServer, frame: Uint8Array): string | null {
  let bytes = frame;
  if (bytes.length && bytes.at(-1) === 0x0d) bytes = bytes.subarray(0, -1);
  let source: string;
  try {
    source = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return wireDumps(rpcError(null, -32700, "Parse error")) + "\n";
  }
  if (!pyStrip(source)) return null;
  let request: JsonValue;
  try {
    request = parseLossless(source, { allowLoneSurrogates: true });
  } catch {
    return wireDumps(rpcError(null, -32700, "Parse error")) + "\n";
  }
  const response = server.handle(request);
  return response === null ? null : wireDumps(response) + "\n";
}

export async function serve(
  server: McpServer,
  input: AsyncIterable<Uint8Array>,
  write: (response: string) => void | Promise<void>,
): Promise<void> {
  let pending = Buffer.alloc(0);
  for await (const chunk of input) {
    pending = Buffer.concat([pending, Buffer.from(chunk)]);
    while (true) {
      const newline = pending.indexOf(0x0a);
      if (newline < 0) break;
      const frame = pending.subarray(0, newline);
      pending = pending.subarray(newline + 1);
      const response = processFrame(server, frame);
      if (response !== null) await write(response);
    }
  }
  if (pending.length) {
    const response = processFrame(server, pending);
    if (response !== null) await write(response);
  }
}

function packageVersion(): string {
  const packagePath = resolve(dirname(fileURLToPath(import.meta.url)), "..", "package.json");
  return asString(get(object(parseLossless(readFileSync(packagePath, "utf8"))), "version"));
}

function argumentsFrom(argv: string[]): { profile: string; database: string } {
  let profile = "";
  let database = "";
  for (let index = 0; index < argv.length; index++) {
    if (argv[index] === "--profile") profile = argv[++index] ?? "";
    else if (argv[index] === "--db") database = argv[++index] ?? "";
    else throw new Error(`unknown argument: ${argv[index]}`);
  }
  if (!profile || !database) throw new Error("--profile and --db are required");
  return { profile, database };
}

export async function main(argv = process.argv.slice(2)): Promise<void> {
  const argumentsValue = argumentsFrom(argv);
  const clockStart = process.env.TRAJECTA_IDENTITY_MCP_CLOCK_START;
  // Python WorkStore.from_config: a non-blank $TRAJECTA_WORK_ROOT wins over the
  // profile's work_root; a blank or whitespace-only value falls back to the profile.
  const envWorkRoot = process.env.TRAJECTA_WORK_ROOT?.trim();
  const memory = new IdentityMemory(loadProfile(argumentsValue.profile), argumentsValue.database, {
    surface: "mcp",
    displayDatabase: argumentsValue.database,
    ...(clockStart ? { clock: new InjectedClock(clockStart) } : {}),
    ...(envWorkRoot ? { workRoot: envWorkRoot } : {}),
  });
  const server = new McpServer(memory, packageVersion());
  await serve(server, process.stdin, async (response) => {
    if (!process.stdout.write(Buffer.from(response, "utf8")))
      await new Promise<void>((done) => process.stdout.once("drain", done));
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    process.stderr.write(`${error instanceof Error ? error.stack : String(error)}\n`);
    process.exitCode = 1;
  });
}
