import "../../node/src/sqlite-warning.ts";
import { readSync, writeSync } from "node:fs";
const { MemoryStore, InjectedClock } = await import("../../node/src/index.ts");
const { hooks } = await import("../../node/src/internal-hooks.ts");
const clock = new InjectedClock();
const store = new MemoryStore(process.argv[2], { now: () => clock.seconds() });
hooks.afterCreateRevisionInsert = () => {
  writeSync(1, "ready\n");
  const release = Buffer.alloc(1);
  if (readSync(0, release, 0, 1, null) !== 1 || release[0] !== 82) {
    throw new Error("holder released without the parent handshake");
  }
};
store.createCurrent({
  recordId: "p16-holder",
  recordClass: "belief",
  domain: "fact",
  title: "Synthetic holder",
  actor: "synthetic",
  reason: "P16 contention",
  evidence: { source_ref: "synthetic:p16" },
  idempotencyKey: "p16-holder",
});
store.close();
