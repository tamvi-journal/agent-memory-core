/**
 * Test-only seams. Not exported from index.ts, so they are not public API.
 * `beforeOpen` runs after the header preflight and before SQLite opens the
 * file, so a test can swap the file in that window (TOCTOU) and prove the
 * post-open recheck is authoritative.
 */
export const hooks: { beforeOpen: ((path: string) => void) | null } = { beforeOpen: null };
