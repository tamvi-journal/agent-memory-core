import assert from "node:assert/strict";
import test from "node:test";
import { StoreBusy } from "../src/errors.ts";
import { translateSqliteError } from "../src/sqlite-errors.ts";

for (const errcode of [5, 6, 261, 262, 517, 518, 773]) {
  test(`P16 SQLite code ${errcode}`, () => {
    const error = Object.assign(new Error("private SQLite detail"), { errcode });
    const translated = translateSqliteError(error);
    assert.ok(translated instanceof StoreBusy);
    assert.equal(translated.message, "store is busy; retry later");
  });
}
for (const errcode of [undefined, 1, 19, "5"]) {
  test(`P16 preserves unrelated error ${errcode}`, () => {
    const error = Object.assign(new Error("database is locked"), { errcode });
    assert.equal(translateSqliteError(error), error);
  });
}
