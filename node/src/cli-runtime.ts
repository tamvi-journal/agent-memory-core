/** Owner CLI. No production confirmation bypass, bootstrap cache, or dependencies. */
import "./sqlite-warning.ts";
import { UnicodeDecodeError } from "./cli-decode.ts";
import { readSync } from "node:fs";
import { resolve, normalize } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { pythonIndentedJson, type Terminal } from "./authority.ts";
import { type Clock } from "./clock.ts";
import { parseIntToken, argparseNegative } from "./cli-tables.ts";
import { doctor } from "./doctor.ts";
import { pyStrip, type JsonValue } from "./encoding.ts";
import {
  ConfirmationMismatch,
  ConfirmationMismatch as CM,
  HumanPresenceRequired,
  ReceiptNotFound,
  ReceiptIntegrityError,
  ProposalIntegrityError,
  ProposalDecided,
  StaleAuthority,
  SchemaVersionError,
  PinnedRecordError,
  FileExistsError,
  ValueError,
} from "./errors.ts";
import { IdentityMemory } from "./identity.ts";
import { outputJson } from "./mcp.ts";
import { expandUser, profileDb, resolvedPath, type Environment } from "./paths.ts";
import { listProfiles, resolveProfile, strictUtf8 } from "./recipe.ts";
import { WorkStoreError } from "./work.ts";

const PUBLIC_ERRORS = [
  HumanPresenceRequired,
  ConfirmationMismatch,
  ReceiptNotFound,
  ReceiptIntegrityError,
  ProposalIntegrityError,
  ProposalDecided,
  StaleAuthority,
  SchemaVersionError,
  PinnedRecordError,
  WorkStoreError,
  FileExistsError,
  ValueError,
];
export const errorName = (error: Error): string =>
  error.constructor.name === "MigrationRequired" ? "MigrationRequiredError" : error.constructor.name;
export class CliExit extends Error {
  readonly status: number;
  constructor(status: number, message = "") {
    super(message);
    this.status = status;
  }
}
export function utf8Replace(value: string): Buffer {
  let output = "";
  for (const char of value) {
    const cp = char.codePointAt(0)!;
    output += cp >= 0xd800 && cp <= 0xdfff ? "?" : char;
  }
  return Buffer.from(output, "utf8");
}
function display(value: JsonValue): JsonValue {
  if (typeof value === "string") return utf8Replace(value).toString("utf8");
  if (Array.isArray(value)) return value.map(display);
  if (value && typeof value === "object" && value.kind === "object")
    return {
      kind: "object",
      entries: value.entries.map(([key, item]) => [utf8Replace(key).toString("utf8"), display(item)]),
    };
  return value;
}
export const cliJson = (value: unknown): string => pythonIndentedJson(display(outputJson(value)), 2) + "\n";

type Grammar = {
  positionals?: string[];
  values?: string[];
  required?: string[];
  flags?: string[];
  defaults?: Record<string, unknown>;
};
const GRAMMAR: Record<string, Grammar> = {
  init: {},
  setup: { values: ["name"] },
  profiles: {},
  work: {},
  status: {},
  doctor: {},
  retrieve: {
    positionals: ["cue"],
    values: ["limit", "budget"],
    flags: ["history", "readonly", "packet"],
    defaults: { limit: 10, budget: 2400 },
  },
  "log-phase": {
    positionals: ["event_id"],
    values: ["title", "summary", "content", "follows", "caused-by", "depends-on", "source"],
    required: ["title", "summary"],
    flags: ["open-loop"],
    defaults: { content: "", source: "" },
  },
  "log-fact": {
    positionals: ["fact_id"],
    values: ["title", "summary", "content", "source"],
    required: ["title", "summary"],
    defaults: { content: "", source: "" },
  },
  timeline: { values: ["limit"], defaults: { limit: 20 } },
  decay: {},
  "core-proposals": {},
  "approve-core": {
    positionals: ["proposal_id"],
    flags: ["apply", "reject", "issue-only"],
    values: ["note"],
    defaults: { note: "" },
  },
  "approve-retract": { positionals: ["record_id"], values: ["reason"], required: ["reason"], flags: ["issue-only"] },
  "close-legacy-discussion": { values: ["note"], required: ["note"], flags: ["issue-only"] },
  "apply-receipt": { positionals: ["receipt_id"] },
  "close-loop": { positionals: ["record_id"], values: ["note"], required: ["note"] },
  "migrate-to": { positionals: ["target"], flags: ["dry-run"], values: ["backup"] },
};
const dest = (key: string) => key.replaceAll("-", "_");
function usage(message: string): never {
  throw new CliExit(2, `trajecta-identity: error: ${message}`);
}
export function parseArgs(argv: string[]): Record<string, any> {
  const result: Record<string, any> = {};
  let grammar: Grammar = { values: ["profile", "db"] },
    positional = 0,
    ended = false;
  for (let i = 0; i < argv.length; i++) {
    const token = argv[i];
    if (!ended && (token === "--help" || token === "-h"))
      throw new CliExit(0, `Trajecta Identity Memory\nCommands: ${Object.keys(GRAMMAR).join(", ")}`);
    if (!ended && token === "--") {
      if (!result.command) usage("invalid choice: --");
      ended = true;
      continue;
    }
    const negative = argparseNegative(token);
    if (!ended && token.startsWith("-") && token !== "-" && !negative) {
      let key: string, inline: string | undefined;
      if (token.startsWith("-p") && !token.startsWith("--") && !result.command) {
        key = "profile";
        inline = token.length > 2 ? token.slice(2) : undefined;
      } else {
        const parts = token.slice(2).split("=");
        key = parts.shift()!;
        inline = parts.length ? parts.join("=") : undefined;
        const candidates = [...(grammar.values ?? []), ...(grammar.flags ?? [])].filter((name) => name.startsWith(key));
        if (!candidates.includes(key)) {
          if (candidates.length !== 1) usage(`unrecognized or ambiguous argument: ${token}`);
          key = candidates[0];
        }
      }
      if ((grammar.flags ?? []).includes(key)) {
        if (inline !== undefined) usage(`argument --${key}: ignored explicit argument`);
        result[dest(key)] = true;
      } else if ((grammar.values ?? []).includes(key)) {
        const value = inline ?? argv[++i];
        if (
          value === undefined ||
          (value.startsWith("-") && value !== "-" && !argparseNegative(value) && inline === undefined)
        )
          usage(`argument --${key}: expected one argument`);
        if (key === "limit" || key === "budget") {
          try {
            result[key] = parseIntToken(value);
          } catch {
            usage(`argument --${key}: invalid int value`);
          }
        } else result[dest(key)] = value;
      } else usage(`unrecognized arguments: ${token}`);
    } else if (!result.command) {
      if (!GRAMMAR[token]) usage(`invalid choice: ${token}`);
      result.command = token;
      grammar = GRAMMAR[token];
      Object.assign(result, grammar.defaults ?? {});
    } else {
      const name = grammar.positionals?.[positional++];
      if (!name) usage(`unrecognized arguments: ${token}`);
      result[name] = token;
    }
  }
  if (!result.command) usage("the following arguments are required: command");
  for (const required of [...(grammar.positionals ?? []), ...(grammar.required ?? [])])
    if (result[dest(required)] === undefined) usage(`the following arguments are required: ${required}`);
  if (result.command === "approve-core" && Boolean(result.apply) === Boolean(result.reject))
    usage("exactly one of --apply or --reject is required");
  return result;
}
export function confirmationLine(read: () => Uint8Array): string {
  const bytes = read();
  try {
    return strictUtf8(bytes);
  } catch (error) {
    if (error instanceof UnicodeDecodeError) throw new CM("confirmation input was not valid UTF-8");
    throw error;
  }
}
export function processTerminal(write: (value: string) => void): Terminal {
  return {
    stdinTTY: process.stdin.isTTY === true,
    stdoutTTY: process.stdout.isTTY === true,
    write,
    read() {
      return confirmationLine(() => {
        const chunks: Buffer[] = [],
          byte = Buffer.alloc(1);
        // Node's TTY descriptor is nonblocking. Match Python's blocking readline
        // without a second pre-check or receipt issuance attempt.
        const pause = new Int32Array(new SharedArrayBuffer(4));
        while (true) {
          let count: number;
          try {
            count = readSync(0, byte, 0, 1, null);
          } catch (error) {
            if (["EAGAIN", "EWOULDBLOCK", "EINTR"].includes((error as NodeJS.ErrnoException).code ?? "")) {
              Atomics.wait(pause, 0, 0, 10);
              continue;
            }
            throw error;
          }
          if (!count) break;
          chunks.push(Buffer.from(byte));
          if (byte[0] === 10) break;
        }
        return Buffer.concat(chunks);
      });
    },
  };
}
function list(value?: string): string[] {
  return (value ?? "").split(",").map(pyStrip).filter(Boolean);
}
export type CliOptions = {
  env?: Environment;
  clock?: Clock;
  terminal?: Terminal;
  stdout?: (value: Buffer) => void;
  stderr?: (value: Buffer) => void;
};
export async function main(argv = process.argv.slice(2), options: CliOptions = {}): Promise<number> {
  const stdout = (value: string) =>
    (
      options.stdout ??
      ((bytes) => {
        process.stdout.write(bytes);
      })
    )(utf8Replace(value));
  const stderr = (value: string) =>
    (
      options.stderr ??
      ((bytes) => {
        process.stderr.write(bytes);
      })
    )(utf8Replace(value));
  const env = options.env ?? process.env;
  let memory: IdentityMemory | undefined;
  try {
    const args = parseArgs(argv),
      command = args.command;
    if (command === "profiles") {
      stdout(cliJson({ profiles: listProfiles(env) }));
      return 0;
    }
    const profile = await resolveProfile(args.profile, { env, rememberChoice: command !== "doctor" });
    const database = args.db !== undefined ? normalize(args.db) : profileDb(profile.name, env);
    memory = new IdentityMemory(profile, database, {
      surface: "cli",
      displayDatabase: database,
      ...(options.clock ? { clock: options.clock } : {}),
      workRoot: pyStrip(env.TRAJECTA_WORK_ROOT ?? "") || undefined,
    });
    let result: unknown;
    const terminal = options.terminal ?? processTerminal(stdout);
    const settle = (receipt: Record<string, unknown>, consume: (id: string) => unknown) => {
      const id = String(receipt.receipt_id);
      if (args.issue_only) return { receipt_id: id, status: "issued" };
      try {
        return consume(id);
      } catch (error) {
        stderr(id + "\n");
        throw new CliExit(1, `${errorName(error as Error)}: ${(error as Error).message}`);
      }
    };
    switch (command) {
      case "init":
        result = memory.bootstrap();
        break;
      case "setup": {
        const mcpArgs = [
          "--experimental-strip-types",
          fileURLToPath(new URL("./mcp.ts", import.meta.url)),
          "--profile",
          profile.name,
        ];
        if (args.db !== undefined) mcpArgs.push("--db", resolvedPath(args.db, env));
        result = {
          mcpServers: {
            [args.name || `trajecta-identity-${profile.name}`]: { command: process.execPath, args: mcpArgs },
          },
        };
        memory.bootstrap();
        stdout(cliJson(result));
        stderr(
          "\nPaste this into your agent client's MCP config (Claude Desktop: Settings → Developer → Edit Config).\n",
        );
        return 0;
      }
      case "doctor":
        result = doctor(memory.store);
        stdout(cliJson(result));
        return (result as any).passed ? 0 : 1;
      case "status":
        result = memory.status();
        break;
      case "work":
        if (!memory.work) throw new CliExit(1, "no work store: set TRAJECTA_WORK_ROOT or work_root in the profile");
        result = { work_store: memory.work.root, work: memory.work.workItems() };
        break;
      case "retrieve":
        try {
          result = memory.retrieve(args.cue, {
            limit: Number(args.limit),
            tokenBudget: args.budget,
            includeHistory: args.history ? true : undefined,
            track: !args.readonly,
          });
        } catch (error) {
          if (error instanceof RangeError) throw new ValueError(error.message);
          throw error;
        }
        if (args.packet) {
          stdout(String((result as any).packet));
          return 0;
        }
        break;
      case "log-phase":
        memory.bootstrap();
        result = memory.logPhase(args.event_id, {
          title: args.title,
          summary: args.summary,
          content: args.content,
          follows: list(args.follows),
          causedBy: list(args.caused_by),
          dependsOn: list(args.depends_on),
          openLoop: Boolean(args.open_loop),
          sourceRef: args.source,
        });
        break;
      case "log-fact":
        memory.bootstrap();
        result = memory.logFact(args.fact_id, {
          title: args.title,
          summary: args.summary,
          content: args.content,
          sourceRef: args.source,
        });
        break;
      case "timeline":
        result = { timeline: memory.timeline(Number(args.limit)) };
        break;
      case "decay":
        memory.bootstrap();
        result = memory.decay(options.clock?.micros());
        break;
      case "core-proposals":
        result = { open_core_proposals: memory.openCoreProposals() };
        break;
      case "approve-core":
        result = settle(
          memory.issueCoreReceipt(args.proposal_id, args.apply ? "apply" : "reject", args.note, terminal),
          (id) => memory!.coreApply(id),
        );
        break;
      case "approve-retract":
        result = settle(memory.issueRetractReceipt(args.record_id, args.reason, terminal), (id) => memory!.retract(id));
        break;
      case "close-legacy-discussion":
        result = settle(memory.issueLegacyCloseReceipt(args.note, terminal), (id) => memory!.closeLegacyDiscussion(id));
        break;
      case "apply-receipt": {
        memory.store.requireWritable();
        const row = memory.store.all(
          "SELECT purpose FROM memory_owner_receipts_v5 WHERE receipt_id=?",
          args.receipt_id,
        )[0];
        if (!row) throw new ReceiptNotFound(`unknown receipt ${args.receipt_id}`);
        const consumers: Record<string, (id: string) => unknown> = {
          identity_core_revision: (id) => memory!.coreApply(id),
          identity_retract: (id) => memory!.retract(id),
          identity_legacy_discussion_close: (id) => memory!.closeLegacyDiscussion(id),
        };
        const consumer = consumers[String(row.purpose)];
        if (!consumer) throw new ReceiptIntegrityError("unknown receipt purpose");
        result = consumer(args.receipt_id);
        break;
      }
      case "close-loop":
        memory.bootstrap();
        result = memory.closeLoop(args.record_id, args.note);
        break;
      case "migrate-to": {
        const migrated = memory.store.migrateTo(args.target, {
          dryRun: Boolean(args.dry_run),
          backupPath: args.backup,
        });
        if ("schemaInfo" in migrated) {
          result = migrated.schemaInfo();
          migrated.close();
        } else result = migrated;
        break;
      }
    }
    stdout(cliJson(result));
    return 0;
  } catch (error) {
    if (error instanceof CliExit) {
      if (error.message) (error.status === 0 ? stdout : stderr)(error.message + "\n");
      return error.status;
    }
    if (PUBLIC_ERRORS.some((kind) => error instanceof kind)) {
      stderr(`${errorName(error as Error)}: ${(error as Error).message}\n`);
      return 1;
    }
    throw error;
  } finally {
    memory?.close();
  }
}
