/** Test-only batch adapter; public CLI, MCP serve, and writer APIs only. */
import "../../node/src/sqlite-warning.ts";
import { createInterface } from "node:readline";
import { resolve, relative, isAbsolute } from "node:path";
import { realpathSync as realpath, existsSync, readFileSync, statSync, unlinkSync } from "node:fs";
import type { JsonValue } from "../../node/src/encoding.ts";
// Load SQLite only after the existing exact G7 diagnostic filter is installed.
const { main: cliMain } = await import("../../node/src/cli.ts");
const { confirmationLine, errorName } = await import("../../node/src/cli-runtime.ts");
const { McpServer, serve, outputJson } = await import("../../node/src/mcp.ts");
const { IdentityMemory, InjectedClock, loadProfile, parseLossless, pyJsonDumps, get, asString } = await import(
  "../../node/src/index.ts"
);

function native(value: JsonValue): any {
  if (value === null || typeof value !== "object") return value;
  if (Array.isArray(value)) return value.map(native);
  if (value.kind === "int" || value.kind === "float") return value;
  return Object.fromEntries(
    value.entries.map(([key, item]) => [
      key,
      ["phase_context", "source_payload", "vho_stack"].includes(key) ? item : native(item),
    ]),
  );
}
function contain(root: string, path: string): void {
  let parent = path;
  while (!existsSync(parent)) {
    const next = resolve(parent, "..");
    if (next === parent) throw new Error("no existing parent");
    parent = next;
  }
  const part = relative(root, realpath(parent));
  if (isAbsolute(part) || part === ".." || part.startsWith("../") || part.startsWith("..\\"))
    throw new Error("path outside selected root");
}

async function execute(caseValue: any) {
  for (const key of Object.keys(process.env)) delete process.env[key];
  for (const [key, value] of Object.entries(caseValue.env)) process.env[key] = value as string;
  const { hooks } = await import("../../node/src/internal-hooks.ts");
  hooks.beforeBackupVerify =
    caseValue.backup_fault === "set"
      ? () => {
          throw new Error("synthetic backup verification failure");
        }
      : null;
  hooks.readBackupMtimeNs = caseValue.backup_fault === "readback" ? () => 0n : null;
  if (caseValue.backup_fault === "readback-missing") {
    hooks.readBackupMtimeNs = (path) => {
      unlinkSync(path);
      return statSync(path, { bigint: true }).mtimeNs;
    };
  }
  const root = realpath(caseValue.root);
  const database = caseValue.database ?? "store.sqlite3";
  contain(root, resolve(root, database));
  const previousCwd = process.cwd();
  process.chdir(root);
  let stdout = "",
    stderr = "",
    status = 0;
  const units: any[] = [];
  const clock = new InjectedClock(caseValue.clock ?? "2026-09-30T00:00:00+00:00");
  try {
    if (caseValue.surface === "cli") {
      const bytes = Buffer.from(caseValue.stdin ?? "", "base64");
      const terminal = caseValue.tty
        ? {
            stdinTTY: true,
            stdoutTTY: true,
            write: (value: string) => {
              stdout += value;
            },
            read: () => confirmationLine(() => bytes),
          }
        : { stdinTTY: false, stdoutTTY: false, write: () => {}, read: () => "" };
      status = await cliMain(caseValue.argv, {
        env: caseValue.env,
        clock,
        terminal,
        stdout: (bytes) => {
          stdout += bytes.toString("utf8");
        },
        stderr: (bytes) => {
          stderr += bytes.toString("utf8");
        },
      });
    } else {
      const memory = new IdentityMemory(
        loadProfile(resolve(caseValue.env.TRAJECTA_IDENTITY_PROFILES, "example/profile.json")),
        database,
        {
          surface: caseValue.surface === "mcp" ? "mcp" : "golden",
          clock,
          displayDatabase: database,
        },
      );
      const { instrument } = await import("./instrument.ts");
      const dispose = instrument(memory.store, clock, caseValue, units);
      try {
        if (caseValue.surface === "mcp") {
          const version = asString(
            get(parseLossless(readFileSync(new URL("../../node/package.json", import.meta.url), "utf8")), "version"),
          );
          const server = new McpServer(memory, version);
          const chunks = caseValue.chunks.map((chunk: string) => Buffer.from(chunk, "base64"));
          async function* input() {
            for (const chunk of chunks) yield chunk;
          }
          const previousWrite = process.stderr.write;
          process.stderr.write = ((value: string | Uint8Array) => {
            stderr += typeof value === "string" ? value : Buffer.from(value).toString("utf8");
            return true;
          }) as typeof process.stderr.write;
          try {
            await serve(server, input(), (value) => {
              stdout += value;
            });
          } finally {
            process.stderr.write = previousWrite;
          }
        } else {
          const args = caseValue.arguments ?? {};
          let result: unknown;
          try {
            switch (caseValue.call) {
              case "initialize":
                result = memory.store.initialize();
                break;
              case "bootstrap":
                result = memory.bootstrap();
                break;
              case "create_current":
                result = memory.store.createCurrent({
                  recordId: args.record_id,
                  recordClass: args.record_class,
                  domain: args.domain,
                  title: args.title,
                  summary: args.summary,
                  content: args.content,
                  actor: args.actor,
                  reason: args.reason,
                  evidence: args.evidence,
                  idempotencyKey: args.idempotency_key,
                  impact: args.impact,
                  confidence: args.confidence,
                  salience: args.salience,
                  stability: args.stability,
                  accessibility: args.accessibility,
                  authorityStatus: args.authority_status,
                  scope: args.scope,
                  surface: args.surface,
                  modelFamily: args.model_family,
                });
                break;
              case "intake_submit": {
                const { ValidatedIntake, DEFAULT_POLICY } = await import("../../node/src/governance.ts");
                result = new ValidatedIntake(memory.store, { surface: "golden", policy: DEFAULT_POLICY }).submit(
                  args.proposal,
                );
                break;
              }
              case "runtime_submit":
                result = memory.runtime.submit(args.proposal);
                break;
              case "identity_core_propose":
                result = memory.corePropose({ ...args, phaseContext: args.phase_context });
                break;
              case "owner_approve_core":
                result = memory.issueCoreReceipt(args.proposal_id, args.outcome, args.decision_note ?? "", {
                  stdinTTY: true,
                  stdoutTTY: true,
                  read: () => `${args.outcome.toUpperCase()} ${args.proposal_id.split(":")[1].slice(0, 12)}`,
                });
                break;
              case "identity_core_apply":
                result = memory.coreApply(args.receipt_id);
                break;
              case "owner_approve_retract":
                result = memory.issueRetractReceipt(args.record_id, args.reason, {
                  stdinTTY: true,
                  stdoutTTY: true,
                  read: () => `RETRACT ${args.record_id}`,
                });
                break;
              case "identity_close_legacy_discussion":
                result = memory.closeLegacyDiscussion(args.receipt_id);
                break;
              case "identity_retract":
                result = memory.retract(args.receipt_id);
                break;
              case "revise":
                result = memory.store.revise({
                  recordId: args.record_id,
                  operationType: args.operation_type,
                  actor: args.actor,
                  reason: args.reason,
                  evidence: args.evidence,
                  idempotencyKey: args.idempotency_key,
                  changes: args.changes,
                  surface: args.surface,
                });
                break;
              case "invalidate":
                result = memory.store.invalidate({
                  recordId: args.record_id,
                  actor: args.actor,
                  reason: args.reason,
                  evidence: args.evidence,
                  surface: args.surface,
                  idempotencyKey: args.idempotency_key,
                });
                break;
              case "add_relation":
              case "retract_relation":
                result = memory.store[caseValue.call === "add_relation" ? "addRelation" : "retractRelation"]({
                  relationId: args.relation_id,
                  fromRecordId: args.from_record_id,
                  toRecordId: args.to_record_id,
                  relationType: args.relation_type,
                  weight: args.weight,
                  actor: args.actor,
                  reason: args.reason,
                  idempotencyKey: args.idempotency_key,
                });
                break;
              case "maintenance":
                result = memory.store.applyMaintenance({
                  runId: args.run_id,
                  adjustments: args.adjustments.map((a: any) => ({
                    recordId: a.record_id,
                    field: a.field,
                    newValue: a.new_value,
                    oldValue: a.old_value,
                  })),
                  actor: args.actor,
                  reason: args.reason,
                  surface: args.surface,
                  idempotencyKey: args.idempotency_key,
                });
                break;
              case "retrieve":
                result = memory.retrieve(args.cue, {
                  track: args.track,
                  limit: Number(args.limit?.value ?? args.limit ?? 10),
                });
                break;
              case "decay":
                result = memory.decay(args.now);
                break;
              case "log_phase":
                result = memory.logPhase(args.event_id, {
                  title: args.title,
                  summary: args.summary,
                  content: args.content,
                  follows: args.follows,
                  causedBy: args.caused_by,
                  dependsOn: args.depends_on,
                  cues: args.cues,
                  evidence: args.evidence,
                  confidence: args.confidence,
                  decidedBecause: args.decided_because,
                  openLoop: args.open_loop,
                  workRefs: args.work_refs,
                  sourceRef: args.source_ref,
                  phaseContext: args.phase_context,
                  occurredAt: args.occurred_at,
                });
                break;
              case "log_fact":
                result = memory.logFact(args.fact_id, {
                  title: args.title,
                  summary: args.summary,
                  content: args.content,
                  cues: args.cues,
                  evidence: args.evidence,
                  confidence: args.confidence,
                  causedBy: args.caused_by,
                  dependsOn: args.depends_on,
                  sourceRef: args.source_ref,
                });
                break;
              default:
                throw new Error("unknown harness kernel call");
            }
            stdout += pyJsonDumps(outputJson(result));
          } catch (error) {
            stdout += `${errorName(error as Error)}: ${(error as Error).message}`;
          }
        }
      } finally {
        dispose();
        memory.close();
      }
    }
  } catch (error) {
    status = 1;
    stderr += `${(error as Error).stack ?? error}\n`;
  } finally {
    // Windows holds a process's current directory open. Release this case
    // before replying so the parent can remove it while batch mode stays alive.
    process.chdir(previousCwd);
  }
  return {
    exit: status,
    worker_cwd: process.cwd(),
    units,
    stdout: Buffer.from(stdout).toString("base64"),
    stderr: Buffer.from(stderr).toString("base64"),
  };
}

for await (const line of createInterface({ input: process.stdin, crlfDelay: Infinity })) {
  const parsed = parseLossless(line);
  const caseValue = native(parsed);
  const result = await execute(caseValue);
  process.stdout.write(pyJsonDumps(outputJson(result)) + "\n");
}
