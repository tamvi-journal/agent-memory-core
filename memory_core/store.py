from __future__ import annotations

import hashlib
import json
import re
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .text import normalize_identity_v1, normalize_text


APPLICATION_ID = 0x414D4333  # "AMC3"
SCHEMA_VERSION = 5
READABLE_SCHEMA_VERSIONS = frozenset({4, 5})
WRITABLE_SCHEMA_VERSION = 5
LEGACY_V4_VERSION = 4
LEGACY_V3_VERSION = 3
EVIDENCE_IDENTITY_VERSION = "evidence-v2"


class SchemaVersionError(RuntimeError):
    """The database is not compatible with this Memory Core runtime."""


class MigrationRequiredError(SchemaVersionError):
    """The database is a recognized legacy store and needs explicit migration."""


class IncompatibleJournalMode(SchemaVersionError):
    """The store must be explicitly repaired before a read can continue."""


class PinnedRecordError(ValueError):
    """A public identity writer attempted to mutate a structural record."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_payload(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def semantic_hash(value: dict[str, Any]) -> str:
    return hash_payload(
        {
            "title": value.get("title", ""),
            "summary": value.get("summary", ""),
            "content": value.get("content", ""),
            "impact": value.get("impact", ""),
            "confidence": float(value.get("confidence", 0.7)),
            "authority_status": value.get(
                "authority_status", "canonical_reference"
            ),
        }
    )


def clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _identity_segment(value: str, fallback: str) -> str:
    normalized = normalize_identity_v1(value).strip()
    normalized = re.sub(r"[^a-z0-9._/-]+", "-", normalized).strip("-")
    return normalized or fallback


def canonical_evidence_identity(
    evidence: dict[str, Any],
    *,
    source_sha256: str | None = None,
) -> dict[str, str]:
    source_ref = str(evidence.get("source_ref", "")).strip()
    inferred_family = source_ref.split(":", 1)[0] if ":" in source_ref else source_ref
    source_family = _identity_segment(
        str(evidence.get("source_family", inferred_family)),
        "unknown-source",
    )
    independence_group = _identity_segment(
        str(evidence.get("independence_group", source_ref or source_family)),
        source_family,
    )
    if source_sha256 is None:
        source_payload = evidence.get(
            "source_payload",
            {
                "source_ref": source_ref,
                "content_summary": evidence.get("content_summary", ""),
            },
        )
        source_sha256 = hash_payload(source_payload)
    identity_version = str(
        evidence.get("identity_version", EVIDENCE_IDENTITY_VERSION)
    )
    identity = {
        "identity_version": identity_version,
        "source_family": source_family,
        "independence_group": independence_group,
        "source_sha256": source_sha256,
    }
    return {
        **identity,
        "evidence_sha256": hash_payload(identity),
    }


class MemoryStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        pinned_guard: tuple[str, ...] = (),
    ):
        self.db_path = Path(db_path)
        self.pinned_guard = frozenset(pinned_guard)
        self._bootstrap_depth = 0

    def _guard_pinned(self, record_id: str) -> None:
        if self._bootstrap_depth == 0 and record_id in self.pinned_guard:
            raise PinnedRecordError(f"public writer refuses pinned record_id {record_id!r}")

    def require_writable(self, *, allow_uninitialized: bool = False) -> dict[str, Any]:
        """Reject a public mutation before validation, replay, or writable open."""

        info = self.schema_info()
        state = info["state"]
        if state == "ready" or (allow_uninitialized and state == "uninitialized"):
            return info
        if state == "legacy-v4":
            raise MigrationRequiredError(
                "schema v4 store must be migrated to v5 before writing"
            )
        if state in {"legacy-v2", "legacy-v3"}:
            raise MigrationRequiredError(f"{state} store requires migration")
        if state == "incompatible":
            raise SchemaVersionError(
                "database application_id/user_version is newer or foreign"
            )
        raise SchemaVersionError(f"store state {state!r} is not writable")

    @contextmanager
    def _bootstrap_writes(self) -> Iterator[None]:
        """Private bootstrap-only exception to the identity pinned guard."""

        self._bootstrap_depth += 1
        try:
            yield
        finally:
            self._bootstrap_depth -= 1

    def _preflight(self, *, readonly: bool) -> None:
        """Refuse WAL and foreign files before SQLite ever opens them.

        Opening a WAL database, even ``mode=ro``, can create ``-wal``/``-shm``
        sidecars, and a writable open of a file this store does not own could
        leave them behind on a crash. So the decision is made from the file
        header bytes alone; the PRAGMA checks after open stay as defense in
        depth.
        """

        repair = "Repair with: sqlite3 <store.sqlite3> 'PRAGMA journal_mode=DELETE;'"
        resolved = self.db_path.resolve()
        if readonly and any(
            Path(f"{resolved}{suffix}").exists() for suffix in ("-wal", "-shm")
        ):
            raise IncompatibleJournalMode(f"SQLite WAL sidecar present. {repair}")
        try:
            with open(resolved, "rb") as handle:
                header = handle.read(100)
        except FileNotFoundError:
            return
        if len(header) < 100 or header[:16] != b"SQLite format 3\x00":
            # Empty or not a SQLite file: nothing to decide from the header;
            # SQLite itself reports it (an empty file is an empty database).
            return
        wal = header[18] == 2 or header[19] == 2
        user_version = int.from_bytes(header[60:64], "big", signed=True)
        application_id = int.from_bytes(header[68:72], "big", signed=True)
        if readonly:
            if wal:
                raise IncompatibleJournalMode(f"SQLite journal_mode=wal. {repair}")
            return
        if application_id not in {0, APPLICATION_ID} or user_version > SCHEMA_VERSION:
            raise SchemaVersionError(
                "database application_id/user_version is newer or foreign"
            )

    @contextmanager
    def _raw_connect(self, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
        repair = "Repair with: sqlite3 <store.sqlite3> 'PRAGMA journal_mode=DELETE;'"
        resolved = self.db_path.resolve()
        self._preflight(readonly=readonly)
        if readonly:
            uri = f"{resolved.as_uri()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            if readonly:
                if mode == "wal":
                    raise IncompatibleJournalMode(f"SQLite journal_mode=wal. {repair}")
            elif mode != "delete":
                # Never rewrite the header of a database this store does not
                # own: a foreign or future file is refused untouched.
                application_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
                user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                if application_id not in {0, APPLICATION_ID} or user_version > SCHEMA_VERSION:
                    raise SchemaVersionError(
                        "database application_id/user_version is newer or foreign"
                    )
                try:
                    mode = str(conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower()
                except sqlite3.Error as exc:
                    raise IncompatibleJournalMode(
                        f"SQLite refused journal_mode=DELETE. {repair}"
                    ) from exc
                if mode != "delete":
                    raise IncompatibleJournalMode(
                        f"SQLite refused journal_mode=DELETE (got {mode}). {repair}"
                    )
            if readonly:
                yield conn
            else:
                with conn:
                    yield conn
        finally:
            conn.close()

    @contextmanager
    def connect(
        self,
        *,
        readonly: bool = False,
        require_schema: bool = True,
    ) -> Iterator[sqlite3.Connection]:
        if readonly and not self.db_path.exists():
            raise FileNotFoundError(self.db_path)
        with self._raw_connect(readonly=readonly) as conn:
            if require_schema:
                self._assert_schema(conn, writable=not readonly)
            yield conn

    def schema_info(self) -> dict[str, Any]:
        if not self.db_path.exists():
            return {
                "application_id": 0,
                "user_version": 0,
                "state": "uninitialized",
            }
        with self._raw_connect(readonly=True) as conn:
            application_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
            user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        if application_id == APPLICATION_ID and user_version == SCHEMA_VERSION:
            state = "ready"
        elif user_version > SCHEMA_VERSION or (
            application_id not in {0, APPLICATION_ID}
        ):
            state = "incompatible"
        elif application_id == APPLICATION_ID and user_version == LEGACY_V4_VERSION:
            state = "legacy-v4"
        elif "memory_records_v2" in tables:
            state = "legacy-v2"
        elif (
            application_id == APPLICATION_ID
            and user_version == LEGACY_V3_VERSION
        ):
            state = "legacy-v3"
        else:
            state = "unknown"
        return {
            "application_id": application_id,
            "user_version": user_version,
            "state": state,
        }

    def initialize(self, *, migrate: bool = True) -> dict[str, Any]:
        """Create the current schema or migrate a recognized v2/v3 store.

        Ordinary reads never call this method. The compatibility alias `init`
        remains for existing consumers, but it is only used on write paths.
        """

        # initialize is a writable open, so reconcile a persisted WAL header
        # before schema_info performs its fail-closed read.
        with self._raw_connect():
            pass
        before = self.schema_info()
        if before["state"] == "ready":
            return {**before, "changed": False}
        if before["state"] == "incompatible":
            raise SchemaVersionError(
                "database application_id/user_version is newer or foreign"
            )
        if before["state"] == "legacy-v4":
            raise MigrationRequiredError(
                "schema v4 store must be migrated to v5 before writing"
            )
        if before["state"] in {"legacy-v2", "legacy-v3"} and not migrate:
            raise MigrationRequiredError(
                f"{before['state']} store requires migration"
            )

        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        with self._raw_connect() as conn:
            application_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
            user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if user_version > SCHEMA_VERSION:
                raise SchemaVersionError(
                    f"database user_version {user_version} exceeds {SCHEMA_VERSION}"
                )
            if application_id not in {0, APPLICATION_ID}:
                raise SchemaVersionError(
                    f"foreign application_id {application_id}; expected {APPLICATION_ID}"
                )
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            conn.executescript(schema)
            if "memory_records_v2" in tables:
                self._migrate_v2(conn)
            self._backfill_relation_events(conn)
            conn.execute(
                "INSERT INTO memory_meta_v3(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        after = self.schema_info()
        return {
            **after,
            "changed": True,
            "migrated_from": {
                "legacy-v2": "v2",
                "legacy-v3": "v3",
            }.get(before["state"]),
        }

    def init(self) -> None:
        self.initialize()

    def migrate_to(
        self,
        target_path: str | Path,
        *,
        dry_run: bool = False,
        backup_path: str | Path | None = None,
    ) -> "MemoryStore | dict[str, Any]":
        """Rehearse or create an explicit migrated copy; never replace source.

        For v4 the migration is additive: execute the v5 ``IF NOT EXISTS``
        schema on the copy and then advance ``user_version``.  An actual run
        also keeps a byte-for-byte v4 backup next to the requested target (or
        at ``backup_path``).  A dry run uses a temporary copy and leaves no
        output behind.
        """

        target = Path(target_path)
        if target.exists():
            raise FileExistsError(target)
        if not self.db_path.exists():
            if dry_run:
                return {"state": "ready", "from": "uninitialized", "dry_run": True}
            migrated = MemoryStore(target, pinned_guard=tuple(self.pinned_guard))
            migrated.initialize()
            return migrated
        before = self.schema_info()
        if before["state"] not in {"legacy-v4", "legacy-v3", "legacy-v2"}:
            raise MigrationRequiredError(
                f"store state {before['state']!r} is not an explicit migration source"
            )

        def migrate_copy(copy_path: Path) -> MemoryStore:
            shutil.copy2(self.db_path, copy_path)
            migrated_store = MemoryStore(
                copy_path, pinned_guard=tuple(self.pinned_guard)
            )
            if before["state"] == "legacy-v4":
                schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
                with migrated_store._raw_connect() as conn:
                    conn.executescript(schema)
                    conn.execute(
                        "INSERT INTO memory_meta_v3(key,value) VALUES('schema_version',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(SCHEMA_VERSION),),
                    )
                    conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
                    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            else:
                migrated_store.initialize()
            if migrated_store.schema_info()["state"] != "ready":
                raise RuntimeError("migration copy did not reach schema v5")
            return migrated_store

        if dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="trajecta-migrate-dry-run-", dir=target.parent) as tmp:
                rehearsal = migrate_copy(Path(tmp) / target.name)
                return {
                    "state": rehearsal.schema_info()["state"],
                    "from": before["state"],
                    "dry_run": True,
                }

        source_version = before["state"].rsplit("v", 1)[1]
        backup = (
            Path(backup_path)
            if backup_path is not None
            else target.with_name(f"{target.name}.v{source_version}.bak")
        )
        if os.path.normcase(os.path.abspath(backup)) == os.path.normcase(os.path.abspath(target)):
            raise ValueError("backup path must differ from target")
        if backup.exists():
            raise FileExistsError(backup)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.db_path, backup)
        # Outside cleanup: target may alias the durable backup through its parent.
        if target.exists():
            raise FileExistsError(target)
        try:
            return migrate_copy(target)
        except Exception:
            target.unlink(missing_ok=True)
            raise

    def _assert_schema(self, conn: sqlite3.Connection, *, writable: bool) -> None:
        application_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if application_id == APPLICATION_ID and user_version == SCHEMA_VERSION:
            return
        if user_version > SCHEMA_VERSION or application_id not in {
            0,
            APPLICATION_ID,
        }:
            raise SchemaVersionError(
                "database is newer than this runtime or belongs to another application"
            )
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if application_id == APPLICATION_ID and user_version == LEGACY_V4_VERSION:
            if writable:
                raise MigrationRequiredError(
                    "schema v4 store must be migrated to v5 before writing"
                )
            return
        if "memory_records_v2" in tables:
            raise MigrationRequiredError(
                "legacy v2 store must be initialized or migrated before use"
            )
        if (
            application_id == APPLICATION_ID
            and user_version == LEGACY_V3_VERSION
        ):
            raise MigrationRequiredError(
                "schema v3 store must be initialized or migrated to v5 before use"
            )
        raise SchemaVersionError("memory database is not initialized")

    def current_view(self, record_id: str | None = None) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        with self.connect(readonly=True) as conn:
            if record_id:
                rows = conn.execute(
                    "SELECT * FROM memory_current_v3 WHERE record_id=?",
                    (record_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM memory_current_v3 ORDER BY domain,record_id"
                ).fetchall()
        return [dict(row) for row in rows]

    def historical_view(self, record_id: str) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        with self.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT * FROM memory_revision_state_v3 WHERE record_id=? "
                "ORDER BY revision_number",
                (record_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def evidence_for_revision(
        self, revision_id: str
    ) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        with self.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT e.*,l.stance,l.weight,l.reason AS link_reason "
                "FROM memory_revision_evidence_v3 l "
                "JOIN memory_evidence_v3 e ON e.evidence_id=l.evidence_id "
                "WHERE l.revision_id=? "
                "ORDER BY e.captured_at,e.evidence_id,l.stance",
                (revision_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def cue_rows(self, profile: str, scope: str) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        with self.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT * FROM memory_cues_v3 WHERE profile=? "
                "AND scope IN ('global',?) ORDER BY weight DESC, cue_id",
                (profile, scope),
            ).fetchall()
        return [dict(row) for row in rows]

    def active_relation_rows(self) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        with self.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT * FROM memory_relation_current_v4 "
                "ORDER BY relation_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def create_current(
        self,
        *,
        record_id: str,
        record_class: str,
        domain: str,
        title: str,
        summary: str = "",
        content: str = "",
        impact: str = "",
        confidence: float = 0.7,
        salience: float = 0.5,
        stability: float = 0.5,
        accessibility: float = 0.5,
        authority_status: str = "canonical_reference",
        scope: str = "global",
        actor: str,
        surface: str = "",
        model_family: str = "",
        reason: str,
        evidence: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        self.require_writable()
        self._guard_pinned(record_id)
        self.initialize()
        with self.connect() as conn:
            prior = self._operation_by_key(conn, idempotency_key)
            if prior:
                return self._operation_result(conn, prior)
            if conn.execute(
                "SELECT 1 FROM memory_records_v3 WHERE record_id=?", (record_id,)
            ).fetchone():
                raise ValueError("record already exists")
            now = utc_now()
            conn.execute(
                "INSERT INTO memory_records_v3("
                "record_id,record_class,domain,scope,created_at,created_by"
                ") VALUES(?,?,?,?,?,?)",
                (record_id, record_class, domain, scope, now, actor),
            )
            evidence_id = self._insert_evidence(conn, evidence)
            revision_id = self._revision_id(record_id, 1, idempotency_key)
            revision = {
                "revision_id": revision_id,
                "record_id": record_id,
                "parent_revision_id": None,
                "revision_number": 1,
                "title": title,
                "summary": summary,
                "content": content,
                "impact": impact,
                "confidence": clamp(confidence),
                "valid_from": now,
                "authority_status": authority_status,
                "created_at": now,
                "created_by": actor,
                "surface": surface,
                "model_family": model_family,
                "reason": reason,
                "idempotency_key": idempotency_key,
            }
            revision["content_sha256"] = semantic_hash(revision)
            self._insert_revision(conn, revision)
            self._insert_telemetry(
                conn,
                revision_id,
                salience=salience,
                stability=stability,
                accessibility=accessibility,
                now=now,
            )
            self._link_evidence(conn, revision_id, evidence_id, "supports", reason)
            operation = self._insert_operation(
                conn,
                operation_type="create",
                actor=actor,
                surface=surface,
                record_id=record_id,
                revision_id=revision_id,
                evidence_ids=[evidence_id],
                decision="materialized",
                reason=reason,
                details={"record_class": record_class, "domain": domain},
                idempotency_key=idempotency_key,
            )
            self._append_lifecycle(
                conn,
                record_id=record_id,
                revision_id=revision_id,
                state="current",
                actor=actor,
                surface=surface,
                reason=reason,
                operation_id=operation["operation_id"],
                idempotency_key=f"{idempotency_key}:lifecycle:current",
                effective_at=now,
            )
            return self._operation_result(conn, operation)

    def revise(
        self,
        record_id: str,
        *,
        operation_type: str,
        actor: str,
        reason: str,
        evidence: dict[str, Any],
        idempotency_key: str,
        changes: dict[str, Any],
        surface: str = "",
        model_family: str = "",
    ) -> dict[str, Any]:
        self.require_writable()
        self._guard_pinned(record_id)
        if operation_type not in {"correct", "refine", "supersede"}:
            raise ValueError("unsupported semantic operation")
        semantic_fields = {
            "title",
            "summary",
            "content",
            "impact",
            "confidence",
            "authority_status",
            "valid_from",
        }
        telemetry_fields = {"salience", "stability", "accessibility"}
        unknown = set(changes) - semantic_fields - telemetry_fields
        if unknown:
            raise ValueError(
                "unsupported revision fields: " + ", ".join(sorted(unknown))
            )
        self.initialize()
        with self.connect() as conn:
            prior = self._operation_by_key(conn, idempotency_key)
            if prior:
                return self._operation_result(conn, prior)
            current = conn.execute(
                "SELECT * FROM memory_current_v3 WHERE record_id=?", (record_id,)
            ).fetchone()
            if not current:
                raise ValueError("current record not found")
            now = utc_now()
            next_number = int(current["revision_number"]) + 1
            revision_id = self._revision_id(
                record_id, next_number, idempotency_key
            )
            revision = {
                key: current[key]
                for key in (
                    "title",
                    "summary",
                    "content",
                    "impact",
                    "confidence",
                    "valid_from",
                    "authority_status",
                )
            }
            revision.update(
                {key: value for key, value in changes.items() if key in semantic_fields}
            )
            revision.update(
                {
                    "revision_id": revision_id,
                    "record_id": record_id,
                    "parent_revision_id": current["revision_id"],
                    "revision_number": next_number,
                    "created_at": now,
                    "created_by": actor,
                    "surface": surface,
                    "model_family": model_family,
                    "reason": reason,
                    "idempotency_key": idempotency_key,
                }
            )
            revision["confidence"] = clamp(revision["confidence"])
            revision["content_sha256"] = semantic_hash(revision)
            evidence_id = self._insert_evidence(conn, evidence)
            self._insert_revision(conn, revision)
            self._insert_telemetry(
                conn,
                revision_id,
                salience=changes.get("salience", current["salience"]),
                stability=changes.get("stability", current["stability"]),
                accessibility=changes.get("accessibility", current["accessibility"]),
                now=now,
            )
            self._link_evidence(conn, revision_id, evidence_id, "supports", reason)
            operation = self._insert_operation(
                conn,
                operation_type=operation_type,
                actor=actor,
                surface=surface,
                record_id=record_id,
                revision_id=revision_id,
                evidence_ids=[evidence_id],
                decision="materialized",
                reason=reason,
                details={"parent_revision_id": current["revision_id"]},
                idempotency_key=idempotency_key,
            )
            self._append_lifecycle(
                conn,
                record_id=record_id,
                revision_id=current["revision_id"],
                state="superseded",
                actor=actor,
                surface=surface,
                reason=reason,
                operation_id=operation["operation_id"],
                idempotency_key=f"{idempotency_key}:lifecycle:superseded",
                effective_at=now,
            )
            self._append_lifecycle(
                conn,
                record_id=record_id,
                revision_id=revision_id,
                state="current",
                actor=actor,
                surface=surface,
                reason=reason,
                operation_id=operation["operation_id"],
                idempotency_key=f"{idempotency_key}:lifecycle:current",
                effective_at=now,
            )
            return self._operation_result(conn, operation)

    def invalidate(
        self,
        record_id: str,
        *,
        actor: str,
        reason: str,
        evidence: dict[str, Any],
        idempotency_key: str,
        surface: str = "",
    ) -> dict[str, Any]:
        self.require_writable()
        self._guard_pinned(record_id)
        self.initialize()
        with self.connect() as conn:
            prior = self._operation_by_key(conn, idempotency_key)
            if prior:
                return self._operation_result(conn, prior)
            current = conn.execute(
                "SELECT * FROM memory_current_v3 WHERE record_id=?", (record_id,)
            ).fetchone()
            if not current:
                raise ValueError("current record not found")
            evidence_id = self._insert_evidence(conn, evidence)
            now = utc_now()
            operation = self._insert_operation(
                conn,
                operation_type="invalidate",
                actor=actor,
                surface=surface,
                record_id=record_id,
                revision_id=current["revision_id"],
                evidence_ids=[evidence_id],
                decision="materialized",
                reason=reason,
                details={},
                idempotency_key=idempotency_key,
            )
            self._link_evidence(
                conn, current["revision_id"], evidence_id, "contradicts", reason
            )
            self._append_lifecycle(
                conn,
                record_id=record_id,
                revision_id=current["revision_id"],
                state="invalidated",
                actor=actor,
                surface=surface,
                reason=reason,
                operation_id=operation["operation_id"],
                idempotency_key=f"{idempotency_key}:lifecycle:invalidated",
                effective_at=now,
            )
            return self._operation_result(conn, operation)

    def _revise_in(
        self,
        conn: sqlite3.Connection,
        record_id: str,
        *,
        actor: str,
        reason: str,
        idempotency_key: str,
        changes: dict[str, Any],
        evidence_ids: list[str],
        operation_id: str,
        surface: str = "",
        model_family: str = "",
    ) -> str:
        """Materialize a revision on an already-held authority transaction."""

        current = conn.execute(
            "SELECT * FROM memory_current_v3 WHERE record_id=?", (record_id,)
        ).fetchone()
        if not current:
            raise ValueError("current record not found")
        now = utc_now()
        next_number = int(current["revision_number"]) + 1
        revision_id = self._revision_id(record_id, next_number, idempotency_key)
        revision = {
            key: current[key]
            for key in (
                "title", "summary", "content", "impact", "confidence",
                "valid_from", "authority_status",
            )
        }
        revision.update({
            key: value for key, value in changes.items()
            if key in {"title", "summary", "content", "impact", "confidence", "authority_status", "valid_from"}
        })
        revision.update({
            "revision_id": revision_id,
            "record_id": record_id,
            "parent_revision_id": current["revision_id"],
            "revision_number": next_number,
            "created_at": now,
            "created_by": actor,
            "surface": surface,
            "model_family": model_family,
            "reason": reason,
            "idempotency_key": idempotency_key,
        })
        revision["confidence"] = clamp(revision["confidence"])
        revision["content_sha256"] = semantic_hash(revision)
        self._insert_revision(conn, revision)
        self._insert_telemetry(
            conn,
            revision_id,
            salience=changes.get("salience", current["salience"]),
            stability=changes.get("stability", current["stability"]),
            accessibility=changes.get("accessibility", current["accessibility"]),
            now=now,
        )
        for evidence_id in evidence_ids:
            self._link_evidence(conn, revision_id, evidence_id, "supports", reason)
        self._append_lifecycle(
            conn,
            record_id=record_id,
            revision_id=current["revision_id"],
            state="superseded",
            actor=actor,
            surface=surface,
            reason=reason,
            operation_id=operation_id,
            idempotency_key=f"{idempotency_key}:lifecycle:superseded",
            effective_at=now,
        )
        self._append_lifecycle(
            conn,
            record_id=record_id,
            revision_id=revision_id,
            state="current",
            actor=actor,
            surface=surface,
            reason=reason,
            operation_id=operation_id,
            idempotency_key=f"{idempotency_key}:lifecycle:current",
            effective_at=now,
        )
        return revision_id

    def _invalidate_in(
        self,
        conn: sqlite3.Connection,
        record_id: str,
        *,
        actor: str,
        reason: str,
        idempotency_key: str,
        evidence_ids: list[str],
        operation_id: str,
        surface: str = "",
    ) -> str:
        """Append invalidation state on an already-held authority transaction."""

        current = conn.execute(
            "SELECT * FROM memory_current_v3 WHERE record_id=?", (record_id,)
        ).fetchone()
        if not current:
            raise ValueError("current record not found")
        for evidence_id in evidence_ids:
            self._link_evidence(
                conn, current["revision_id"], evidence_id, "contradicts", reason
            )
        self._append_lifecycle(
            conn,
            record_id=record_id,
            revision_id=current["revision_id"],
            state="invalidated",
            actor=actor,
            surface=surface,
            reason=reason,
            operation_id=operation_id,
            idempotency_key=f"{idempotency_key}:lifecycle:invalidated",
            effective_at=utc_now(),
        )
        return str(current["revision_id"])

    def _retract_relation_in(
        self,
        conn: sqlite3.Connection,
        *,
        latest: sqlite3.Row,
        actor: str,
        surface: str,
        reason: str,
        idempotency_key: str,
    ) -> sqlite3.Row:
        """Append a relation retraction without creating a second operation."""

        sequence = int(latest["sequence_number"]) + 1
        event_id = "relation-event:" + hashlib.sha256(
            idempotency_key.encode("utf-8")
        ).hexdigest()[:32]
        conn.execute(
            "INSERT INTO memory_relation_events_v4("
            "relation_event_id,relation_id,from_record_id,to_record_id,"
            "relation_type,sequence_number,event_type,weight,"
            "source_revision_id,evidence_id,actor,surface,reason,"
            "idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id, latest["relation_id"], latest["from_record_id"],
                latest["to_record_id"], latest["relation_type"], sequence,
                "retract", 0.0, None, None, actor, surface, reason,
                idempotency_key, utc_now(),
            ),
        )
        return conn.execute(
            "SELECT * FROM memory_relation_events_v4 WHERE relation_event_id=?",
            (event_id,),
        ).fetchone()

    def add_cue(
        self,
        *,
        profile: str,
        cue: str,
        target_record_id: str,
        weight: float = 1.0,
        cue_type: str = "phrase",
        scope: str = "global",
    ) -> None:
        self.require_writable()
        self.initialize()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO memory_cues_v3("
                "cue,cue_norm,cue_type,target_record_id,weight,scope,profile"
                ") VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(profile,cue_norm,target_record_id) DO UPDATE SET "
                "cue=excluded.cue,cue_type=excluded.cue_type,"
                "weight=excluded.weight,scope=excluded.scope",
                (
                    cue,
                    normalize_text(cue),
                    cue_type,
                    target_record_id,
                    float(weight),
                    scope,
                    profile,
                ),
            )

    def add_relation(
        self,
        *,
        relation_id: str,
        from_record_id: str,
        to_record_id: str,
        relation_type: str,
        weight: float = 1.0,
        source_revision_id: str | None = None,
        actor: str = "host",
        surface: str = "",
        reason: str = "relation asserted",
        evidence: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Assert a relation by appending an event. Never rewrites history.

        Re-asserting an unchanged relation is a no-op. A changed weight or
        source appends a new ``assert`` event; the earlier one stays readable
        through :meth:`relation_history`.
        """

        self.require_writable()
        return self._append_relation_event(
            event_type="assert",
            relation_id=relation_id,
            from_record_id=from_record_id,
            to_record_id=to_record_id,
            relation_type=relation_type,
            weight=weight,
            source_revision_id=source_revision_id,
            actor=actor,
            surface=surface,
            reason=reason,
            evidence=evidence,
            idempotency_key=idempotency_key,
        )

    def retract_relation(
        self,
        *,
        from_record_id: str,
        to_record_id: str,
        relation_type: str,
        actor: str,
        reason: str,
        surface: str = "",
        evidence: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Retract an active relation by appending a ``retract`` event."""

        self.require_writable()
        if not str(actor).strip() or not str(reason).strip():
            raise ValueError("relation retraction requires actor and reason")
        return self._append_relation_event(
            event_type="retract",
            relation_id=None,
            from_record_id=from_record_id,
            to_record_id=to_record_id,
            relation_type=relation_type,
            weight=0.0,
            source_revision_id=None,
            actor=actor,
            surface=surface,
            reason=reason,
            evidence=evidence,
            idempotency_key=idempotency_key,
        )

    def relation_history(
        self,
        *,
        from_record_id: str,
        to_record_id: str,
        relation_type: str,
    ) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        with self.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT * FROM memory_relation_events_v4 "
                "WHERE from_record_id=? AND to_record_id=? AND relation_type=? "
                "ORDER BY sequence_number",
                (from_record_id, to_record_id, relation_type),
            ).fetchall()
        return [dict(row) for row in rows]

    def _append_relation_event(
        self,
        *,
        event_type: str,
        relation_id: str | None,
        from_record_id: str,
        to_record_id: str,
        relation_type: str,
        weight: float,
        source_revision_id: str | None,
        actor: str,
        surface: str,
        reason: str,
        evidence: dict[str, Any] | None,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        relation_type = str(relation_type).strip()
        if not relation_type:
            raise ValueError("relation_type must be non-empty")
        if from_record_id == to_record_id:
            raise ValueError("a relation needs two different records")
        weight = float(weight)
        if event_type == "assert" and not 0.0 <= weight <= 10.0:
            raise ValueError("relation weight must be between 0 and 10")
        self.initialize()
        with self.connect() as conn:
            if idempotency_key:
                prior = conn.execute(
                    "SELECT * FROM memory_relation_events_v4 "
                    "WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if prior:
                    return {**dict(prior), "status": "duplicate"}
            latest = conn.execute(
                "SELECT * FROM memory_relation_events_v4 "
                "WHERE from_record_id=? AND to_record_id=? AND relation_type=? "
                "ORDER BY sequence_number DESC LIMIT 1",
                (from_record_id, to_record_id, relation_type),
            ).fetchone()
            if event_type == "retract":
                if not latest or latest["event_type"] == "retract":
                    return {
                        "status": "no_op",
                        "reason": "relation is not active",
                        "relation_id": latest["relation_id"] if latest else None,
                    }
            elif (
                latest
                and latest["event_type"] == "assert"
                and abs(float(latest["weight"]) - weight) <= 1e-9
                and latest["source_revision_id"] == source_revision_id
            ):
                return {**dict(latest), "status": "no_op"}
            stable_id = (
                latest["relation_id"]
                if latest
                else (str(relation_id or "").strip() or self._relation_key(
                    from_record_id, to_record_id, relation_type
                ))
            )
            sequence = int(latest["sequence_number"]) + 1 if latest else 1
            evidence_id = None
            if evidence:
                item = dict(evidence)
                item.setdefault("actor", actor)
                item.setdefault("surface", surface)
                evidence_id = self._insert_evidence(conn, item)
            key = idempotency_key or (
                f"relation:{stable_id}:{sequence}:{event_type}"
            )
            event_id = "relation-event:" + hashlib.sha256(
                key.encode("utf-8")
            ).hexdigest()[:32]
            conn.execute(
                "INSERT INTO memory_relation_events_v4("
                "relation_event_id,relation_id,from_record_id,to_record_id,"
                "relation_type,sequence_number,event_type,weight,"
                "source_revision_id,evidence_id,actor,surface,reason,"
                "idempotency_key,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    stable_id,
                    from_record_id,
                    to_record_id,
                    relation_type,
                    sequence,
                    event_type,
                    weight,
                    source_revision_id,
                    evidence_id,
                    actor,
                    surface,
                    reason,
                    key,
                    utc_now(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM memory_relation_events_v4 "
                "WHERE relation_event_id=?",
                (event_id,),
            ).fetchone()
        status = "retracted" if event_type == "retract" else (
            "asserted" if sequence == 1 or latest["event_type"] == "retract"
            else "reweighted"
        )
        return {**dict(row), "status": status}

    @staticmethod
    def _relation_key(from_record_id: str, to_record_id: str, relation_type: str) -> str:
        return "relation:" + hash_payload(
            [from_record_id, to_record_id, relation_type]
        )[:32]

    def _backfill_relation_events(self, conn: sqlite3.Connection) -> None:
        """Carry mutable v3 relation rows into the v4 event stream once."""

        if not self._table_exists(conn, "memory_relations_v3"):
            return
        rows = conn.execute(
            "SELECT * FROM memory_relations_v3 ORDER BY created_at,relation_id"
        ).fetchall()
        for row in rows:
            exists = conn.execute(
                "SELECT 1 FROM memory_relation_events_v4 "
                "WHERE from_record_id=? AND to_record_id=? AND relation_type=?",
                (row["from_record_id"], row["to_record_id"], row["relation_type"]),
            ).fetchone()
            if exists:
                continue
            events = [("assert", float(row["weight"]))]
            if row["status"] != "active":
                events.append(("retract", 0.0))
            for sequence, (event_type, weight) in enumerate(events, start=1):
                key = f"migrate-v3:{row['relation_id']}:{event_type}"
                conn.execute(
                    "INSERT INTO memory_relation_events_v4("
                    "relation_event_id,relation_id,from_record_id,to_record_id,"
                    "relation_type,sequence_number,event_type,weight,"
                    "source_revision_id,evidence_id,actor,surface,reason,"
                    "idempotency_key,created_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,?)",
                    (
                        "relation-event:" + hashlib.sha256(
                            key.encode("utf-8")
                        ).hexdigest()[:32],
                        row["relation_id"],
                        row["from_record_id"],
                        row["to_record_id"],
                        row["relation_type"],
                        sequence,
                        event_type,
                        weight,
                        row["source_revision_id"],
                        "memory-core-migration",
                        "migration",
                        "carried from mutable v3 relation row",
                        key,
                        row["created_at"],
                    ),
                )

    def record_access(
        self,
        *,
        cue: str,
        record_id: str,
        revision_id: str,
        retrieval_reason: str,
        rank: int,
        surface: str,
        gain: float = 0.01,
    ) -> None:
        self.require_writable()
        gain = float(gain)
        if not 0.0 <= gain <= 1.0:
            raise ValueError("access gain must be between 0 and 1")
        self.initialize()
        with self.connect() as conn:
            owner = conn.execute(
                "SELECT record_id FROM memory_revisions_v3 WHERE revision_id=?",
                (revision_id,),
            ).fetchone()
            if not owner or owner["record_id"] != record_id:
                raise ValueError("revision does not belong to record")
            now = utc_now()
            conn.execute(
                "INSERT INTO memory_access_v3("
                "cue_sha256,record_id,revision_id,retrieval_reason,rank,"
                "surface,created_at"
                ") VALUES(?,?,?,?,?,?,?)",
                (
                    hashlib.sha256(cue.encode("utf-8")).hexdigest(),
                    record_id,
                    revision_id,
                    retrieval_reason,
                    int(rank),
                    surface,
                    now,
                ),
            )
            conn.execute(
                "UPDATE memory_telemetry_v3 "
                "SET accessibility=MIN(1.0,accessibility+?),"
                "access_count=access_count+1,last_accessed_at=?,updated_at=? "
                "WHERE revision_id=?",
                (gain, now, now, revision_id),
            )

    def apply_maintenance(
        self,
        *,
        run_id: str,
        adjustments: list[dict[str, Any]],
        actor: str,
        reason: str,
        surface: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self.require_writable()
        if not run_id.strip():
            raise ValueError("maintenance run_id is required")
        if not reason.strip():
            raise ValueError("maintenance reason is required")
        allowed_fields = {"salience", "stability", "accessibility"}
        operation_key = idempotency_key or f"maintenance:{run_id}"
        self.initialize()
        with self.connect() as conn:
            prior = self._operation_by_key(conn, operation_key)
            if prior:
                return self._operation_result(conn, prior)
            applied: list[dict[str, Any]] = []
            semantic_hashes: dict[str, str] = {}
            for adjustment in adjustments:
                record_id = str(adjustment.get("record_id", "")).strip()
                field = str(adjustment.get("field", "")).strip()
                if not record_id:
                    raise ValueError("maintenance record_id is required")
                if field not in allowed_fields:
                    raise ValueError(
                        f"unsupported maintenance field: {field or '<empty>'}"
                    )
                current = conn.execute(
                    "SELECT * FROM memory_current_v3 WHERE record_id=?",
                    (record_id,),
                ).fetchone()
                if not current:
                    raise ValueError(
                        f"maintenance current record not found: {record_id}"
                    )
                old_value = float(current[field])
                if adjustment.get("old_value") is not None:
                    expected = float(adjustment["old_value"])
                    if abs(expected - old_value) > 1e-6:
                        raise ValueError(
                            f"maintenance old_value mismatch for {record_id}.{field}"
                        )
                new_value = clamp(float(adjustment["new_value"]))
                semantic_hashes[current["revision_id"]] = current["content_sha256"]
                if abs(new_value - old_value) <= 1e-9:
                    continue
                conn.execute(
                    f"UPDATE memory_telemetry_v3 SET {field}=?,updated_at=? "
                    "WHERE revision_id=?",
                    (new_value, utc_now(), current["revision_id"]),
                )
                applied.append(
                    {
                        "record_id": record_id,
                        "revision_id": current["revision_id"],
                        "field": field,
                        "old_value": old_value,
                        "new_value": new_value,
                    }
                )
            # Test-only fault seam used by the cross-runtime oracle corpus.
            hook = getattr(self, "_test_before_maintenance_hash_check", None)
            if hook is not None:
                hook(conn)
            for revision_id, expected_hash in semantic_hashes.items():
                row = conn.execute(
                    "SELECT content_sha256 FROM memory_revisions_v3 "
                    "WHERE revision_id=?",
                    (revision_id,),
                ).fetchone()
                if not row or row["content_sha256"] != expected_hash:
                    raise RuntimeError("maintenance changed semantic content hash")
            operation = self._insert_operation(
                conn,
                operation_type="maintenance",
                actor=actor,
                surface=surface,
                record_id=applied[0]["record_id"] if len(applied) == 1 else None,
                revision_id=(
                    applied[0]["revision_id"] if len(applied) == 1 else None
                ),
                evidence_ids=[],
                decision="materialized" if applied else "no_op",
                reason=reason,
                details={
                    "run_id": run_id,
                    "adjustments": applied,
                    "semantic_content_changed": False,
                },
                idempotency_key=operation_key,
            )
            return self._operation_result(conn, operation)

    @staticmethod
    def _revision_id(
        record_id: str, revision_number: int, idempotency_key: str
    ) -> str:
        suffix = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:12]
        return f"{record_id}@r{revision_number}-{suffix}"

    @staticmethod
    def _insert_revision(
        conn: sqlite3.Connection, revision: dict[str, Any]
    ) -> None:
        fields = (
            "revision_id",
            "record_id",
            "parent_revision_id",
            "revision_number",
            "title",
            "summary",
            "content",
            "impact",
            "confidence",
            "valid_from",
            "authority_status",
            "content_sha256",
            "created_at",
            "created_by",
            "surface",
            "model_family",
            "reason",
            "idempotency_key",
        )
        conn.execute(
            "INSERT INTO memory_revisions_v3("
            + ",".join(fields)
            + ") VALUES("
            + ",".join(f":{field}" for field in fields)
            + ")",
            revision,
        )

    @staticmethod
    def _insert_telemetry(
        conn: sqlite3.Connection,
        revision_id: str,
        *,
        salience: float,
        stability: float,
        accessibility: float,
        now: str,
    ) -> None:
        conn.execute(
            "INSERT INTO memory_telemetry_v3("
            "revision_id,salience,stability,accessibility,access_count,"
            "last_accessed_at,updated_at"
            ") VALUES(?,?,?,?,0,NULL,?)",
            (
                revision_id,
                clamp(salience),
                clamp(stability),
                clamp(accessibility),
                now,
            ),
        )

    @staticmethod
    def _insert_evidence(
        conn: sqlite3.Connection, evidence: dict[str, Any]
    ) -> str:
        for field in (
            "evidence_type", "source_ref", "source_family", "independence_group",
            "captured_at", "actor", "surface", "model_family", "content_summary",
            "privacy_class", "identity_version",
        ):
            if field in evidence and not isinstance(evidence[field], str):
                raise ValueError(f"evidence.{field} must be a string")
        identity = canonical_evidence_identity(evidence)
        evidence_id = (
            f"evidence:{identity['identity_version']}:"
            f"{identity['evidence_sha256'][:32]}"
        )
        conn.execute(
            "INSERT INTO memory_evidence_v3("
            "evidence_id,identity_version,evidence_type,source_ref,"
            "source_family,independence_group,source_sha256,evidence_sha256,"
            "captured_at,actor,surface,model_family,content_summary,confidence,"
            "privacy_class"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(identity_version,evidence_sha256) DO NOTHING",
            (
                evidence_id,
                identity["identity_version"],
                evidence.get("evidence_type", "observation"),
                evidence.get("source_ref", ""),
                identity["source_family"],
                identity["independence_group"],
                identity["source_sha256"],
                identity["evidence_sha256"],
                evidence.get("captured_at") or utc_now(),
                evidence.get("actor", ""),
                evidence.get("surface", ""),
                evidence.get("model_family", ""),
                evidence.get("content_summary", ""),
                clamp(evidence.get("confidence", 0.7)),
                evidence.get("privacy_class", "private"),
            ),
        )
        row = conn.execute(
            "SELECT evidence_id FROM memory_evidence_v3 "
            "WHERE identity_version=? AND evidence_sha256=?",
            (identity["identity_version"], identity["evidence_sha256"]),
        ).fetchone()
        if not row:
            raise RuntimeError("canonical evidence insert failed")
        return str(row["evidence_id"])

    @staticmethod
    def _link_evidence(
        conn: sqlite3.Connection,
        revision_id: str,
        evidence_id: str,
        stance: str,
        reason: str,
    ) -> None:
        conn.execute(
            "INSERT INTO memory_revision_evidence_v3("
            "revision_id,evidence_id,stance,weight,reason"
            ") VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING",
            (revision_id, evidence_id, stance, 1.0, reason),
        )

    @staticmethod
    def _operation_by_key(
        conn: sqlite3.Connection, idempotency_key: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM memory_operations_v3 WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()

    @staticmethod
    def _insert_operation(
        conn: sqlite3.Connection,
        *,
        operation_type: str,
        actor: str,
        surface: str,
        record_id: str | None,
        revision_id: str | None,
        evidence_ids: list[str],
        decision: str,
        reason: str,
        details: dict[str, Any],
        idempotency_key: str,
    ) -> sqlite3.Row:
        operation_id = "operation:" + hashlib.sha256(
            idempotency_key.encode("utf-8")
        ).hexdigest()[:32]
        conn.execute(
            "INSERT INTO memory_operations_v3("
            "operation_id,operation_type,actor,surface,target_record_id,"
            "target_revision_id,evidence_ids_json,decision,reason,"
            "details_json,idempotency_key,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                operation_id,
                operation_type,
                actor,
                surface,
                record_id,
                revision_id,
                json.dumps(evidence_ids, ensure_ascii=False),
                decision,
                reason,
                json.dumps(details, ensure_ascii=False, sort_keys=True),
                idempotency_key,
                utc_now(),
            ),
        )
        return conn.execute(
            "SELECT * FROM memory_operations_v3 WHERE operation_id=?",
            (operation_id,),
        ).fetchone()

    @staticmethod
    def _append_lifecycle(
        conn: sqlite3.Connection,
        *,
        record_id: str,
        revision_id: str,
        state: str,
        actor: str,
        surface: str,
        reason: str,
        operation_id: str | None,
        idempotency_key: str,
        effective_at: str,
    ) -> None:
        sequence = int(
            conn.execute(
                "SELECT COALESCE(MAX(sequence_number),0)+1 "
                "FROM memory_lifecycle_events_v3 WHERE record_id=?",
                (record_id,),
            ).fetchone()[0]
        )
        event_id = "lifecycle:" + hashlib.sha256(
            idempotency_key.encode("utf-8")
        ).hexdigest()[:32]
        conn.execute(
            "INSERT INTO memory_lifecycle_events_v3("
            "lifecycle_event_id,record_id,revision_id,sequence_number,"
            "lifecycle_state,effective_at,actor,surface,reason,operation_id,"
            "idempotency_key,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                record_id,
                revision_id,
                sequence,
                state,
                effective_at,
                actor,
                surface,
                reason,
                operation_id,
                idempotency_key,
                utc_now(),
            ),
        )

    @staticmethod
    def _operation_result(
        conn: sqlite3.Connection, operation: sqlite3.Row
    ) -> dict[str, Any]:
        result = dict(operation)
        result["evidence_ids"] = json.loads(
            result.pop("evidence_ids_json") or "[]"
        )
        result["details"] = json.loads(result.pop("details_json") or "{}")
        revision = None
        if result["target_revision_id"]:
            row = conn.execute(
                "SELECT * FROM memory_revision_state_v3 WHERE revision_id=?",
                (result["target_revision_id"],),
            ).fetchone()
            revision = dict(row) if row else None
        result["revision"] = revision
        return result

    def _migrate_v2(self, conn: sqlite3.Connection) -> None:
        migrated = conn.execute(
            "SELECT value FROM memory_meta_v3 WHERE key='migrated_from_v2'"
        ).fetchone()
        if migrated:
            return

        records = conn.execute(
            "SELECT * FROM memory_records_v2 ORDER BY record_id"
        ).fetchall()
        for row in records:
            conn.execute(
                "INSERT OR IGNORE INTO memory_records_v3("
                "record_id,record_class,domain,scope,created_at,created_by"
                ") VALUES(?,?,?,?,?,?)",
                (
                    row["record_id"],
                    row["record_class"],
                    row["domain"],
                    row["scope"],
                    row["created_at"],
                    row["created_by"],
                ),
            )

        revisions = conn.execute(
            "SELECT * FROM memory_revisions_v2 "
            "ORDER BY record_id,revision_number"
        ).fetchall()
        for row in revisions:
            revision = dict(row)
            semantic = {
                key: revision[key]
                for key in (
                    "revision_id",
                    "record_id",
                    "parent_revision_id",
                    "revision_number",
                    "title",
                    "summary",
                    "content",
                    "impact",
                    "confidence",
                    "valid_from",
                    "authority_status",
                    "content_sha256",
                    "created_at",
                    "created_by",
                    "surface",
                    "model_family",
                    "reason",
                    "idempotency_key",
                )
            }
            self._insert_revision(conn, semantic)
            conn.execute(
                "INSERT INTO memory_telemetry_v3("
                "revision_id,salience,stability,accessibility,access_count,"
                "last_accessed_at,updated_at"
                ") VALUES(?,?,?,?,0,NULL,?)",
                (
                    revision["revision_id"],
                    revision["salience"],
                    revision["stability"],
                    revision["accessibility"],
                    revision["created_at"],
                ),
            )

        evidence_map: dict[str, str] = {}
        for row in conn.execute(
            "SELECT * FROM memory_evidence_v2 ORDER BY evidence_id"
        ):
            legacy = dict(row)
            identity = canonical_evidence_identity(
                {
                    **legacy,
                    "identity_version": EVIDENCE_IDENTITY_VERSION,
                    "source_family": (
                        legacy["source_ref"].split(":", 1)[0]
                        if legacy["source_ref"]
                        else "legacy"
                    ),
                    "independence_group": legacy["source_ref"] or "legacy",
                },
                source_sha256=legacy["source_sha256"],
            )
            evidence_id = (
                f"evidence:{identity['identity_version']}:"
                f"{identity['evidence_sha256'][:32]}"
            )
            conn.execute(
                "INSERT INTO memory_evidence_v3("
                "evidence_id,identity_version,evidence_type,source_ref,"
                "source_family,independence_group,source_sha256,evidence_sha256,"
                "captured_at,actor,surface,model_family,content_summary,confidence,"
                "privacy_class"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(identity_version,evidence_sha256) DO NOTHING",
                (
                    evidence_id,
                    identity["identity_version"],
                    legacy["evidence_type"],
                    legacy["source_ref"],
                    identity["source_family"],
                    identity["independence_group"],
                    identity["source_sha256"],
                    identity["evidence_sha256"],
                    legacy["captured_at"],
                    legacy["actor"],
                    legacy["surface"],
                    legacy["model_family"],
                    legacy["content_summary"],
                    legacy["confidence"],
                    legacy["privacy_class"],
                ),
            )
            canonical = conn.execute(
                "SELECT evidence_id FROM memory_evidence_v3 "
                "WHERE identity_version=? AND evidence_sha256=?",
                (identity["identity_version"], identity["evidence_sha256"]),
            ).fetchone()
            evidence_map[legacy["evidence_id"]] = str(canonical["evidence_id"])

        if self._table_exists(conn, "memory_revision_evidence_v2"):
            for row in conn.execute(
                "SELECT * FROM memory_revision_evidence_v2 "
                "ORDER BY revision_id,evidence_id,stance"
            ):
                conn.execute(
                    "INSERT OR IGNORE INTO memory_revision_evidence_v3("
                    "revision_id,evidence_id,stance,weight,reason"
                    ") VALUES(?,?,?,?,?)",
                    (
                        row["revision_id"],
                        evidence_map[row["evidence_id"]],
                        row["stance"],
                        row["weight"],
                        row["reason"],
                    ),
                )

        for table, fields in (
            (
                "memory_relations",
                (
                    "relation_id",
                    "from_record_id",
                    "to_record_id",
                    "relation_type",
                    "weight",
                    "source_revision_id",
                    "status",
                    "created_at",
                ),
            ),
            (
                "memory_cues",
                (
                    "cue",
                    "cue_norm",
                    "cue_type",
                    "target_record_id",
                    "weight",
                    "scope",
                    "profile",
                ),
            ),
            (
                "memory_access",
                (
                    "cue_sha256",
                    "record_id",
                    "revision_id",
                    "retrieval_reason",
                    "rank",
                    "surface",
                    "created_at",
                ),
            ),
        ):
            old_name = f"{table}_v2"
            new_name = f"{table}_v3"
            if not self._table_exists(conn, old_name):
                continue
            columns = ",".join(fields)
            conn.execute(
                f"INSERT OR IGNORE INTO {new_name}({columns}) "
                f"SELECT {columns} FROM {old_name}"
            )

        if self._table_exists(conn, "memory_operations_v2"):
            for row in conn.execute(
                "SELECT * FROM memory_operations_v2 "
                "ORDER BY created_at,operation_id"
            ):
                evidence_ids = [
                    evidence_map.get(item, item)
                    for item in json.loads(row["evidence_ids_json"] or "[]")
                ]
                conn.execute(
                    "INSERT OR IGNORE INTO memory_operations_v3("
                    "operation_id,operation_type,actor,surface,target_record_id,"
                    "target_revision_id,evidence_ids_json,decision,reason,"
                    "details_json,idempotency_key,created_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        row["operation_id"],
                        row["operation_type"],
                        row["actor"],
                        row["surface"],
                        row["target_record_id"],
                        row["target_revision_id"],
                        json.dumps(evidence_ids, ensure_ascii=False),
                        row["decision"],
                        row["reason"],
                        row["details_json"],
                        row["idempotency_key"],
                        row["created_at"],
                    ),
                )

        if self._table_exists(conn, "memory_intake_v2"):
            for row in conn.execute(
                "SELECT * FROM memory_intake_v2 ORDER BY created_at,intake_id"
            ):
                evidence_ids = [
                    evidence_map.get(item, item)
                    for item in json.loads(row["evidence_ids_json"] or "[]")
                ]
                conn.execute(
                    "INSERT OR IGNORE INTO memory_intake_v3("
                    "intake_id,operation_type,target_record_id,proposal_sha256,"
                    "evidence_ids_json,status,decision_reason,operation_id,actor,"
                    "surface,idempotency_key,created_at,decided_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        row["intake_id"],
                        row["operation_type"],
                        row["target_record_id"],
                        row["proposal_sha256"],
                        json.dumps(evidence_ids, ensure_ascii=False),
                        row["status"],
                        row["decision_reason"],
                        row["operation_id"],
                        row["actor"],
                        row["surface"],
                        row["idempotency_key"],
                        row["created_at"],
                        row["decided_at"],
                    ),
                )

        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in revisions:
            grouped.setdefault(row["record_id"], []).append(row)
        for record_id, history in grouped.items():
            for revision in history:
                base_key = f"migration:v2:{revision['revision_id']}"
                self._append_lifecycle(
                    conn,
                    record_id=record_id,
                    revision_id=revision["revision_id"],
                    state="current",
                    actor="memory-core-migrator",
                    surface="migration",
                    reason="preserve v2 semantic chronology",
                    operation_id=None,
                    idempotency_key=f"{base_key}:current",
                    effective_at=revision["valid_from"] or revision["created_at"],
                )
                state = revision["revision_status"]
                if state != "current":
                    self._append_lifecycle(
                        conn,
                        record_id=record_id,
                        revision_id=revision["revision_id"],
                        state=state,
                        actor="memory-core-migrator",
                        surface="migration",
                        reason="preserve v2 terminal lifecycle state",
                        operation_id=None,
                        idempotency_key=f"{base_key}:{state}",
                        effective_at=revision["valid_to"] or revision["created_at"],
                    )

        conn.execute(
            "INSERT INTO memory_meta_v3(key,value) VALUES('migrated_from_v2',?)",
            (utc_now(),),
        )

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
        return bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ).fetchone()
        )
