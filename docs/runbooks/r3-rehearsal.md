# R3 owner-local rehearsal

Only Ty makes the source copies. Codex and Aux do not inspect live stores or run
this procedure against real data. Work stores are outside R3.

## Prepare locally

1. Choose a new rehearsal folder outside every Git repository. Stop clients that
   write the chosen identity store yourself. Do not copy a database while it is
   being written. Use SQLite's `.backup` command, or a file copy when no WAL/SHM
   sidecars exist. Put the resulting immutable copy in the rehearsal folder.
2. Put a copy of its identity profile in that folder as `profile.json`. It must
   not configure a work store. The runner refuses `work_root`; it never resolves
   it. Do not provide real paths, stores or profile content to an agent.
3. Write `cues.json` locally as a JSON list of strings. Only numeric cue indices
   and counts appear in the report; neither cue text nor cue hashes are reported.
4. Every input must resolve inside the chosen folder, be a regular file and have
   one hard link. Symlink escapes are refused. Keep all further copies local.

## Run yourself

Use Python 3.11 and Node 22. From this repository, invoke:

```sh
python tools/r3/rehearsal.py --root /your/rehearsal-folder \
  --source input.sqlite3 --profile profile.json --cues cues.json
```

The runner builds an allowlisted environment with contained data, profile, home
and temporary directories before each child starts. Proxies and work-root
environment variables are not inherited. Both runtimes receive the same fixed
clock sequence. The original database and sidecars are checked before and after
for bytes, mtime, mode and inventory; atime and ctime are excluded.

The runner first runs `doctor` on further copies. Legacy v2/v3/v4 copies take the
dry-run and migration path with a verified backup; ready v5 copies pin the exact
`migrate-to` refusal. Both branches exercise status, timeline (limit 1000), core
proposals, read-only retrieval, separate tracked retrieval and decay, and reads
across runtimes. All ready stores must pass the seven doctor checks. Unknown,
incompatible or WAL inputs produce a refusal report without mutation.

The file `rehearsal-report.json` contains static step labels, runtime/state/exit,
counts, doctor booleans and SHA-256 digests. Raw outputs and record-derived strings
are compared locally and are not put in the report. The only numerical tolerance
is the settled per-record decay whitelist, after the generation-time margin
check. A mismatch fails the run; the runner does not repair input or change the
comparison law.

An existing report is refused. Keep failed runs local; report only the redacted
result if you choose to share it. The runner prints only a failure category when
an exception occurs, never its private message. The `rehearsal-*` folders contain
further copies and must not be uploaded or committed. Ty deletes the entire
rehearsal folder when done. CI exercises this runner only with synthetic fixtures.

An all-v5 rehearsal proves compatibility without inventing a migration. A real
legacy source additionally proves real-store migration. No real rehearsal was
performed during implementation.
