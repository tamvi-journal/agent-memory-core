/** Load the CLI after installing the exact node:sqlite diagnostic filter (G7). */
import "./sqlite-warning.ts";
import { pathToFileURL } from "node:url";
import type { CliOptions } from "./cli-runtime.ts";
export async function main(argv = process.argv.slice(2), options: CliOptions = {}): Promise<number> {
  return (await import("./cli-runtime.ts")).main(argv, options);
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href)
  main()
    .then((status) => {
      process.exitCode = status;
    })
    .catch(async (error) => {
      const { utf8Replace } = await import("./cli-runtime.ts");
      process.stderr.write(utf8Replace(`${error.stack ?? error}\n`));
      process.exitCode = 1;
    });
