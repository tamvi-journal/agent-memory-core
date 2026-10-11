/**
 * Test-only seams. Not exported from index.ts, so they are not public API.
 * `beforeOpen` runs after the header preflight and before SQLite opens the
 * file, so a test can swap the file in that window (TOCTOU) and prove the
 * post-open recheck is authoritative.
 */
export const hooks: {
  beforeBackupVerify: (() => void) | null;
  readBackupMtimeNs: ((path: string) => bigint) | null;
  beforeOpen: ((path: string) => void) | null;
  afterAuthorityRevisionInsert: (() => void) | null;
  afterCreateRevisionInsert: (() => void) | null;
  afterFirstMaintenanceAdjustment: (() => void) | null;
  afterFirstAccessCommit: (() => void) | null;
  afterIdentitySubmitCommit: (() => void) | null;
  beforeMaintenanceHashCheck: ((database: any) => void) | null;
  beforeDecaySidecarWrite: (() => void) | null;
} = {
  beforeBackupVerify: null,
  readBackupMtimeNs: null,
  beforeOpen: null,
  afterAuthorityRevisionInsert: null,
  afterCreateRevisionInsert: null,
  afterFirstMaintenanceAdjustment: null,
  afterFirstAccessCommit: null,
  afterIdentitySubmitCommit: null,
  beforeMaintenanceHashCheck: null,
  beforeDecaySidecarWrite: null,
};
