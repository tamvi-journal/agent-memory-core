/** G7: suppress only node:sqlite's exact experimental diagnostic on the CLI path. */
const emit = process.emitWarning;
process.emitWarning = function (warning: string | Error, ...args: any[]): void {
  const message = typeof warning === "string" ? warning : warning.message;
  const type =
    typeof args[0] === "string" ? args[0] : (args[0]?.type ?? (warning instanceof Error ? warning.name : "Warning"));
  if (type === "ExperimentalWarning" && message === "SQLite is an experimental feature and might change at any time")
    return;
  Reflect.apply(emit, process, [warning, ...args]);
} as typeof process.emitWarning;
