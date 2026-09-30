import { resolve } from "node:path";
import { IdentityMemory, InjectedClock, MemoryStore, loadProfile } from "../src/index.ts";
import { hooks } from "../src/internal-hooks.ts";

const [mode, database] = process.argv.slice(2);
const root = resolve(import.meta.dirname, "..", "..");
const profile = loadProfile(resolve(root, "trajecta_identity", "profiles", "example", "profile.json"));
if (mode === "create") {
  const store = new MemoryStore(database, { now: () => new InjectedClock().seconds() });
  hooks.afterCreateRevisionInsert = () => process.exit(91);
  store.createCurrent({
    recordId: "crash-create",
    recordClass: "belief",
    domain: "fact",
    title: "Crash",
    actor: "test",
    reason: "crash",
    evidence: { source_ref: "crash:create", content_summary: "crash" },
    idempotencyKey: "crash:create",
  });
} else if (mode === "maintenance") {
  const store = new MemoryStore(database);
  hooks.afterFirstMaintenanceAdjustment = () => process.exit(92);
  store.applyMaintenance({
    runId: "crash-maintenance",
    adjustments: [
      { recordId: "phase:aa", field: "accessibility", newValue: 0.2 },
      { recordId: "phase:bb", field: "accessibility", newValue: 0.3 },
    ],
    actor: "test",
    reason: "crash",
  });
} else if (mode === "retrieve") {
  const memory = new IdentityMemory(profile, database, { surface: "test", clock: new InjectedClock() });
  hooks.afterFirstAccessCommit = () => process.exit(93);
  memory.retrieve("shared", { track: true, limit: 10 });
} else if (mode === "phase") {
  const memory = new IdentityMemory(profile, database, { surface: "test", clock: new InjectedClock() });
  hooks.afterIdentitySubmitCommit = () => process.exit(94);
  memory.logPhase("crash", {
    title: "Crash phase",
    summary: "shared",
    follows: ["phase:base0"],
    openLoop: true,
    cues: ["crash cue"],
  });
} else throw new Error(`unknown crash mode ${mode}`);
