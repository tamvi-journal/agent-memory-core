/** Settled R3 §3.6/§3.7. Backup timestamps alone compare whole microseconds. */
import { copyFileSync, existsSync, statSync, unlinkSync, utimesSync } from "node:fs";
import { hooks } from "./internal-hooks.ts";
import { ValueError } from "./errors.ts";

export function sourceBackupTime(path: string): bigint {
  const ns = statSync(path, { bigint: true }).mtimeNs;
  const upper = (1n << (process.platform === "win32" ? 31n : 32n)) * 1_000_000_000n;
  if (ns < 0n || ns >= upper) {
    throw new ValueError("source mtime is outside the supported backup range");
  }
  return ns;
}

export function copyBackup(source: string, backup: string, ns: bigint): void {
  copyFileSync(source, backup);
  try {
    const sec = ns / 1_000_000_000n;
    const micros = (ns % 1_000_000_000n) / 1000n;
    const midpoint = Number(sec) + Number(micros * 1000n + 500n) / 1e9;
    hooks.beforeBackupTimeSet?.();
    utimesSync(backup, statSync(source).atime, midpoint);
    hooks.afterBackupTimeSet?.(backup);
    if (statSync(backup, { bigint: true }).mtimeNs / 1000n !== ns / 1000n) {
      throw new Error("backup microsecond differs");
    }
  } catch {
    if (existsSync(backup)) unlinkSync(backup);
    throw new ValueError("backup mtime could not be preserved to a microsecond");
  }
}
