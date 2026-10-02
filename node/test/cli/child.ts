/** Test-only TTY/clock seam; the production entry has no environment bypass. */
import { readFileSync } from "node:fs";
import { main } from "../../src/cli.ts";
const { confirmationLine, utf8Replace } = await import("../../src/cli-runtime.ts");
const { IdentityMemory } = await import("../../src/identity.ts");
import { InjectedClock } from "../../src/clock.ts";
import { RuntimeError, StaleAuthority } from "../../src/errors.ts";
import { get, parseLossless } from "../../src/json.ts";
import type { OrderedObject } from "../../src/encoding.ts";
const invocation = parseLossless(readFileSync(process.argv[2], "utf8")) as OrderedObject;
const tty = get(invocation, "tty") === true,
  hook = get(invocation, "hook");
if (hook === "inline-failure") {
  const fail = () => {
    throw new StaleAuthority("target moved after issuance");
  };
  IdentityMemory.prototype.coreApply = fail;
  IdentityMemory.prototype.retract = fail;
  IdentityMemory.prototype.closeLegacyDiscussion = fail;
}
if (hook === "untyped-crash")
  IdentityMemory.prototype.status = () => {
    throw new RuntimeError("test internal crash");
  };
const terminal = tty
  ? {
      stdinTTY: true,
      stdoutTTY: true,
      write: (value: string) => {
        process.stdout.write(utf8Replace(value));
      },
      read: () =>
        confirmationLine(() => {
          const bytes = readFileSync(0),
            newline = bytes.indexOf(10);
          return newline < 0 ? bytes : bytes.subarray(0, newline + 1);
        }),
    }
  : undefined;
try {
  process.exitCode = await main(process.argv.slice(3), { clock: new InjectedClock(), terminal });
} catch (error) {
  process.stderr.write(utf8Replace(`${(error as Error).stack ?? error}\n`));
  process.exitCode = 1;
}
