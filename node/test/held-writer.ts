/** Test-only public writer paused inside its native transaction. */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { resolve } from "node:path";

export async function heldWriter<T>(root: string, env: Record<string, string>, victim: () => Promise<T>): Promise<T> {
  const holder = spawn(
    process.execPath,
    [
      "--experimental-strip-types",
      resolve(import.meta.dirname, "../../tools/r3/holder.ts"),
      resolve(root, "store.sqlite3"),
    ],
    { cwd: root, env, stdio: ["pipe", "pipe", "pipe"] },
  );
  const stderr: Buffer[] = [];
  holder.stderr.on("data", (part) => stderr.push(Buffer.from(part)));
  const finished = new Promise<number | null>((done, reject) => {
    holder.once("close", done);
    holder.once("error", reject);
  });
  try {
    await new Promise<void>((done, reject) => {
      const timer = setTimeout(() => reject(new Error("holder seam timeout")), 15000);
      holder.stdout.once("data", (data) => {
        clearTimeout(timer);
        try {
          assert.equal(data.toString(), "ready\n");
          done();
        } catch (error) {
          reject(error);
        }
      });
      holder.once("exit", () => {
        clearTimeout(timer);
        reject(new Error("holder exited before seam"));
      });
    });
    const result = await victim();
    assert.equal(holder.exitCode, null);
    holder.stdin.end("R");
    assert.equal(await finished, 0, Buffer.concat(stderr).toString());
    assert.equal(Buffer.concat(stderr).length, 0);
    return result;
  } finally {
    if (holder.exitCode === null) holder.kill("SIGKILL");
    await finished;
  }
}
