/** P16: map native SQLite results at the connection/statement boundary. */
import { DatabaseSync } from "node:sqlite";
import { StoreBusy } from "./errors.ts";

export function translateSqliteError(error: unknown): unknown {
  const code = (error as { errcode?: unknown } | null)?.errcode;
  return typeof code === "number" && [5, 6].includes(code & 0xff) ? new StoreBusy() : error;
}

function sqliteCall<T>(action: () => T): T {
  try {
    return action();
  } catch (error) {
    throw translateSqliteError(error);
  }
}

function guarded<T extends object>(target: T, connection = false): T {
  return new Proxy(target, {
    get(object, key) {
      const value = Reflect.get(object, key, object);
      if (typeof value !== "function") return value;
      return (...args: unknown[]) => {
        const result = sqliteCall(() => Reflect.apply(value, object, args));
        return connection && key === "prepare" ? guarded(result) : result;
      };
    },
  });
}

export function openDatabase(...args: ConstructorParameters<typeof DatabaseSync>): DatabaseSync {
  return guarded(
    sqliteCall(() => new DatabaseSync(...args)),
    true,
  );
}
