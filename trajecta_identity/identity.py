"""Self-authored identity memory on top of the memory_core kernel.

Write law (SPEC §1):

- phase  — the agent logs freely; never overwrites; every new reading is a new
  record linked ``later-phase-of`` the earlier one.
- fact   — the agent logs and revises freely; old revisions stay as history.
- core   — the agent revises freely, but each revision opens a discussion that
  surfaces in every packet until it is closed.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from memory_core import (
    MemoryHit,
    GovernancePolicy,
    MemoryRuntime,
    PacketRenderer,
    vho_open_seed,
)
from memory_core.store import hash_payload, utc_now

from .activation import ActivationPolicy, apply_recall, run_decay, state_of
from .authority import (
    ProposalDecided,
    ProposalIntegrityError,
    ReceiptIntegrityError,
    ReceiptNotFound,
    StaleAuthority,
    require_confirmation,
    short_id,
)
from .paths import profile_db
from .work import WorkStore, refs_in
from .profile import (
    CORE_ID,
    DISCUSSION_ANCHOR,
    OPEN_LOOP_ANCHOR,
    VHO_ID,
    VHO_KEYS,
    IdentityProfile,
    core_content,
    validate_core,
)

SELF_AUTHORED_POLICY = GovernancePolicy(
    event_min_confidence=0.0,
    belief_min_confidence=0.0,
    axis_min_confidence=0.0,
    # The agent's own reading is a legitimate source for its core.
    axis_min_independent_sources=1,
    # Nothing is gated. The core's check is the discussion flag, not a hold.
    protected_domains=(),
)
CAUSAL_RELATIONS = ("later-phase-of", "caused-by", "depends-on", "decided-because")
PINNED = (CORE_ID, VHO_ID, DISCUSSION_ANCHOR, OPEN_LOOP_ANCHOR)
_ID = re.compile(r"^[A-Za-z0-9._:@-]{2,120}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest(*parts: Any) -> str:
    return hashlib.sha256(
        json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]


_OCCURRED = re.compile(r"^Occurred at: (.+)$", re.MULTILINE)


def _occurred(row: dict[str, Any]) -> str:
    match = _OCCURRED.search(row.get("content") or "")
    return match.group(1).strip() if match else (row["valid_from"] or row["created_at"])


def _check_id(value: str, what: str) -> str:
    value = str(value).strip()
    if not _ID.match(value):
        raise ValueError(f"{what} must be 2-120 chars of letters, digits, . _ : @ -")
    return value


class IdentityMemory:
    def __init__(
        self,
        profile: IdentityProfile,
        db_path: str | Path | None = None,
        *,
        surface: str = "local",
        activation: ActivationPolicy | None = None,
    ):
        self.profile = profile
        self.db_path = Path(db_path) if db_path else profile_db(profile.name)
        self.surface = surface
        self.activation = activation or ActivationPolicy()
        self.runtime = MemoryRuntime(
            self.db_path,
            profile.memory_profile(),
            surface=surface,
            governance=SELF_AUTHORED_POLICY,
            pinned_guard=PINNED,
        )
        self.store = self.runtime.store
        self.work = WorkStore.from_config(profile.extra)

    # ------------------------------------------------------------------ setup

    def bootstrap(self) -> dict[str, Any]:
        """Idempotent: anchors, the shared VHO seed, and the profile's core."""

        self.store.require_writable(allow_uninitialized=True)
        self.store.initialize()
        results = {}
        with self.store._bootstrap_writes():
            return self._bootstrap_in(results)

    def _bootstrap_in(self, results: dict[str, Any]) -> dict[str, Any]:
        for anchor, title in (
            (DISCUSSION_ANCHOR, "Open core discussions"),
            (OPEN_LOOP_ANCHOR, "Open loops"),
        ):
            results[anchor] = self._submit(
                operation_type="create",
                record_id=anchor,
                record_class="event",
                domain="anchor",
                reason="structural anchor for relations",
                evidence=[self._self_evidence("bootstrap", title)],
                idempotency_key=f"{self.profile.name}:{anchor}:v1",
                changes={
                    "title": title,
                    "summary": title,
                    # Anchors carry structure only and never appear in recall.
                    "authority_status": "non_authoritative",
                    "accessibility": 1.0,
                },
                skip_if_exists=True,
            )
        vho = vho_open_seed(
            actor=self.profile.agent,
            adoption=self.profile.vho_adoption,
            consumer_notes=self.profile.vho_notes,
        )
        if not self.store.current_view(VHO_ID):
            results[VHO_ID] = self.runtime.submit(**vho)["status"]
        else:
            results[VHO_ID] = "exists"
        core = self.profile.core
        errors = validate_core(core)
        if errors:
            raise ValueError("; ".join(errors))
        results[CORE_ID] = self._submit(
            operation_type="create",
            record_id=CORE_ID,
            record_class="axis",
            domain="core",
            reason="seed the agent's self-location",
            falsifier=core["falsifier"],
            evidence=[self._self_evidence(
                core.get("source_ref", f"profile:{self.profile.name}"),
                core["summary"],
            )],
            idempotency_key=f"{self.profile.name}:core:seed:v1",
            changes={
                "title": core["title"],
                "summary": core["summary"],
                "content": core_content(core),
                "confidence": float(core.get("confidence", 0.9)),
                "stability": 0.9,
                "accessibility": 1.0,
            },
            skip_if_exists=True,
        )
        return {"profile": self.profile.name, "db": str(self.db_path), "records": results}

    # ------------------------------------------------------------------ write

    def log_phase(
        self,
        event_id: str,
        *,
        title: str,
        summary: str,
        content: str = "",
        follows: Iterable[str] = (),
        caused_by: Iterable[str] = (),
        depends_on: Iterable[str] = (),
        decided_because: str = "",
        open_loop: bool = False,
        work_refs: Iterable[str] = (),
        cues: Iterable[str] = (),
        source_ref: str = "",
        confidence: float = 0.85,
        phase_context: dict[str, Any] | None = None,
        occurred_at: str | None = None,
        evidence: Iterable[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        """Log one phase. Never supersedes anything."""

        self.store.require_writable()
        record_id = "phase:" + _check_id(event_id, "event_id")
        links = self._require_existing(
            {"later-phase-of": follows, "caused-by": caused_by, "depends-on": depends_on}
        )
        work_refs = [str(ref).strip() for ref in work_refs if str(ref).strip()]
        if work_refs and self.work is not None:
            missing = self.work.missing(work_refs)
            if missing:
                raise ValueError("unknown work refs in trajecta-work-memory: " + ", ".join(missing))
        body = self._compose(content, decided_because, work_refs, phase_context, occurred_at)
        result = self._submit(
            operation_type="create",
            record_id=record_id,
            record_class="event",
            domain="phase",
            reason="self-logged phase",
            evidence=self._evidence(source_ref or f"self:{event_id}", summary, confidence, evidence),
            idempotency_key=f"{self.profile.name}:{record_id}",
            changes={
                "title": title,
                "summary": summary,
                "content": body,
                "impact": decided_because,
                "confidence": confidence,
                "accessibility": self.activation.new_record,
            },
            skip_if_exists=True,
        )
        self._link(record_id, links, reason=summary)
        if open_loop:
            self._relate(record_id, OPEN_LOOP_ANCHOR, "open-loop", reason=summary)
        self._cues(record_id, cues, title)
        status = {"materialized": "logged", "exists": "exists"}.get(result, result)
        return {"record_id": record_id, "status": status, "linked": links}

    def log_fact(
        self,
        fact_id: str,
        *,
        title: str,
        summary: str,
        content: str = "",
        caused_by: Iterable[str] = (),
        depends_on: Iterable[str] = (),
        cues: Iterable[str] = (),
        source_ref: str = "",
        confidence: float = 0.8,
        evidence: Iterable[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        """Create a fact, or revise it. The previous revision stays as history."""

        self.store.require_writable()
        record_id = "fact:" + _check_id(fact_id, "fact_id")
        links = self._require_existing({"caused-by": caused_by, "depends-on": depends_on})
        current = self.store.current_view(record_id) if self.db_path.exists() else []
        exists = bool(current)
        if exists and (current[0]["title"], current[0]["summary"], current[0]["content"]) == (
            title, summary, content
        ):
            self._link(record_id, links, reason=summary)
            return {"record_id": record_id, "status": "no_op"}
        intake = self.runtime.submit(
            operation_type="refine" if exists else "create",
            record_id=record_id,
            record_class=None if exists else "belief",
            domain=None if exists else "fact",
            actor=self.profile.agent,
            reason="self-logged fact",
            logic="the agent recorded what it currently holds",
            truth_basis="provenance is attached",
            evidence=self._evidence(source_ref or f"self:{fact_id}", summary, confidence, evidence),
            idempotency_key=f"{self.profile.name}:{record_id}:{_digest(title, summary, content, confidence)}",
            changes={
                "title": title,
                "summary": summary,
                "content": content,
                "confidence": confidence,
                **({} if exists else {"accessibility": self.activation.new_record}),
            },
        )
        self._link(record_id, links, reason=summary)
        self._cues(record_id, cues, title)
        status = intake["status"]
        if status == "materialized":
            status = "revised" if exists else "created"
        return {"record_id": record_id, "status": status}

    def identity_core_propose(
        self,
        *,
        reason: str,
        phase_context: dict[str, Any],
        title: str | None = None,
        summary: str | None = None,
        vho_stack: dict[str, str] | None = None,
        recognition_signature: list[str] | None = None,
        falsifier: str | None = None,
        source_ref: str = "",
    ) -> dict[str, Any]:
        """Append a proposal; never mutate the canonical core."""

        self.store.require_writable()
        if not str(reason).strip():
            raise ValueError("a core proposal needs a reason")
        if not isinstance(phase_context, dict) or not phase_context:
            raise ValueError("a core proposal needs phase_context (model, harness, policies)")
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT * FROM memory_current_v3 WHERE record_id='core'"
            ).fetchone()
            if not current:
                raise ValueError("core is not bootstrapped")
            old = json.loads(current["content"])
            merged = {
                "title": title or current["title"],
                "summary": summary or current["summary"],
                "vho_stack": {**old["vho_stack"], **(vho_stack or {})},
                "recognition_signature": recognition_signature or old["recognition_signature"],
                "falsifier": falsifier or old["falsifier"],
                "phase_context": phase_context,
            }
            errors = validate_core(merged)
            if errors:
                raise ValueError("; ".join(errors))
            content = core_content(merged)
            phase_context_json = json.dumps(
                phase_context, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
            phase_context_sha256 = hash_payload(phase_context)
            reason_sha256 = hashlib.sha256(reason.encode("utf-8")).hexdigest()
            proposal_fields = {
                "profile": self.profile.name,
                "record_id": CORE_ID,
                "base_core_revision_id": current["revision_id"],
                "title": merged["title"],
                "summary": merged["summary"],
                "content_sha256": content_sha256,
                "phase_context_sha256": phase_context_sha256,
                "reason_sha256": reason_sha256,
            }
            proposal_source = source_ref or (
                "self:core-proposal:" + hashlib.sha256(
                    hash_payload(proposal_fields).encode("utf-8")
                ).hexdigest()[:32]
            )
            proposal_sha256 = hash_payload(
                {**proposal_fields, "source_ref": proposal_source}
            )
            proposal_id = "core-proposal:" + proposal_sha256[:32]
            existing = conn.execute(
                "SELECT * FROM memory_core_proposals_v5 WHERE proposal_sha256=?",
                (proposal_sha256,),
            ).fetchone()
            if existing:
                return {**dict(existing), "status": "existing"}
            conn.execute(
                "INSERT INTO memory_core_proposals_v5("
                "proposal_id,profile,record_id,base_core_revision_id,title,summary,"
                "content,content_sha256,phase_context_json,phase_context_sha256,"
                "reason,reason_sha256,source_ref,proposal_sha256,created_at,created_by,surface"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    proposal_id, self.profile.name, CORE_ID,
                    current["revision_id"], merged["title"], merged["summary"],
                    content, content_sha256, phase_context_json,
                    phase_context_sha256, reason, reason_sha256, proposal_source,
                    proposal_sha256, utc_now(), self.profile.agent, self.surface,
                ),
            )
            row = conn.execute(
                "SELECT * FROM memory_core_proposals_v5 WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            return {**dict(row), "status": "proposed"}

    def open_core_proposals(self) -> list[dict[str, Any]]:
        info = self.store.schema_info()
        if info["state"] == "legacy-v4":
            return []
        if info["state"] != "ready":
            return []
        current = self.store.current_view(CORE_ID)
        current_id = current[0]["revision_id"] if current else None
        with self.store.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT * FROM memory_open_core_proposals_v5 "
                "WHERE profile=? ORDER BY created_at,proposal_id",
                (self.profile.name,),
            ).fetchall()
        return [
            {
                "proposal_id": row["proposal_id"],
                "base_core_revision_id": row["base_core_revision_id"],
                "stale": row["base_core_revision_id"] != current_id,
                "created_at": row["created_at"],
                "created_by": row["created_by"],
                "reason": row["reason"],
                "title": row["title"],
            }
            for row in rows
        ]

    def _issue_receipt(self, purpose: str, binding: dict[str, Any]) -> dict[str, Any]:
        binding_json = json.dumps(
            binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        binding_sha256 = hash_payload(binding)
        receipt_id = "receipt:" + binding_sha256[:32]
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO memory_owner_receipts_v5("
                "receipt_id,purpose,profile,binding_json,binding_sha256,issued_at,"
                "issued_by,authority,guard) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(binding_sha256) DO NOTHING",
                (
                    receipt_id, purpose, self.profile.name, binding_json,
                    binding_sha256, utc_now(), self.profile.owner, "owner",
                    "tty-human-presence/v1",
                ),
            )
            row = conn.execute(
                "SELECT * FROM memory_owner_receipts_v5 WHERE binding_sha256=?",
                (binding_sha256,),
            ).fetchone()
        return dict(row)

    def issue_core_receipt(
        self,
        proposal_id: str,
        *,
        outcome: str,
        decision_note: str = "",
        stdin,
        stdout,
    ) -> dict[str, Any]:
        self.store.require_writable()
        if outcome not in {"apply", "reject"}:
            raise ValueError("outcome must be apply or reject")
        with self.store.connect(readonly=True) as conn:
            proposal = conn.execute(
                "SELECT * FROM memory_core_proposals_v5 WHERE proposal_id=? AND profile=?",
                (proposal_id, self.profile.name),
            ).fetchone()
            if not proposal:
                raise ProposalIntegrityError("proposal not found")
            if conn.execute(
                "SELECT 1 FROM memory_proposal_decisions_v5 WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone():
                raise ProposalDecided("proposal is already decided")
            current = conn.execute(
                "SELECT revision_id FROM memory_current_v3 WHERE record_id='core'"
            ).fetchone()
        if not current:
            raise ProposalIntegrityError("core is not bootstrapped")
        if outcome == "apply" and proposal["base_core_revision_id"] != current["revision_id"]:
            raise StaleAuthority("stale proposal cannot be issued an apply receipt")
        expected = f"{outcome.upper()} {short_id(proposal_id)}"
        require_confirmation(expected, stdin=stdin, stdout=stdout)
        binding = {
            "purpose": "identity_core_revision",
            "profile": self.profile.name,
            "current_core_revision_id": current["revision_id"],
            "proposal_id": proposal_id,
            "proposal_sha256": proposal["proposal_sha256"],
            "content_sha256": proposal["content_sha256"],
            "phase_context_sha256": proposal["phase_context_sha256"],
            "reason_sha256": proposal["reason_sha256"],
            "source_ref": proposal["source_ref"],
            "outcome": outcome,
            "decision_note": decision_note,
            "authority": "owner",
        }
        return self._issue_receipt("identity_core_revision", binding)

    def issue_retract_receipt(
        self, record_id: str, *, reason: str, stdin, stdout
    ) -> dict[str, Any]:
        self.store.require_writable()
        if record_id in PINNED:
            raise ValueError("core, ontology and anchors cannot be retracted")
        current = self.store.current_view(record_id)
        if not current:
            raise ValueError("current record not found")
        require_confirmation(
            f"RETRACT {record_id}", stdin=stdin, stdout=stdout
        )
        binding = {
            "purpose": "identity_retract",
            "profile": self.profile.name,
            "record_id": record_id,
            "current_revision_id": current[0]["revision_id"],
            "reason": reason,
            "reason_sha256": hashlib.sha256(reason.encode("utf-8")).hexdigest(),
            "authority": "owner",
        }
        return self._issue_receipt("identity_retract", binding)

    def issue_legacy_close_receipt(self, *, note: str, stdin, stdout) -> dict[str, Any]:
        self.store.require_writable()
        current = self.store.current_view(CORE_ID)
        with self.store.connect(readonly=True) as conn:
            relation = conn.execute(
                "SELECT * FROM memory_relation_events_v4 WHERE from_record_id=? "
                "AND to_record_id=? AND relation_type='awaiting-discussion' "
                "ORDER BY sequence_number DESC LIMIT 1",
                (CORE_ID, DISCUSSION_ANCHOR),
            ).fetchone()
        if not current or not relation or relation["event_type"] != "assert":
            raise ValueError("no active legacy core discussion")
        require_confirmation(
            f"CLOSE {short_id(relation['relation_event_id'])}",
            stdin=stdin,
            stdout=stdout,
        )
        binding = {
            "purpose": "identity_legacy_discussion_close",
            "profile": self.profile.name,
            "core_revision_id": current[0]["revision_id"],
            "relation_event_id": relation["relation_event_id"],
            "relation_source_revision_id": relation["source_revision_id"],
            "note": note,
            "note_sha256": hashlib.sha256(note.encode("utf-8")).hexdigest(),
            "authority": "owner",
        }
        return self._issue_receipt("identity_legacy_discussion_close", binding)

    def _receipt_in(self, conn, receipt_id: str, purpose: str):
        receipt = conn.execute(
            "SELECT * FROM memory_owner_receipts_v5 WHERE receipt_id=?", (receipt_id,)
        ).fetchone()
        if not receipt:
            raise ReceiptNotFound(f"unknown receipt {receipt_id}")
        try:
            binding = json.loads(receipt["binding_json"])
        except (TypeError, ValueError) as exc:
            raise ReceiptIntegrityError("receipt binding_json is invalid") from exc
        if not isinstance(binding, dict):
            raise ReceiptIntegrityError("receipt binding must be an object")
        digest = hash_payload(binding)
        if digest != receipt["binding_sha256"]:
            raise ReceiptIntegrityError("receipt binding_sha256 mismatch")
        if receipt_id != "receipt:" + digest[:32]:
            raise ReceiptIntegrityError("receipt_id does not match binding")
        if (
            receipt["purpose"] != purpose
            or receipt["profile"] != self.profile.name
            or receipt["authority"] != "owner"
            or binding.get("purpose") != receipt["purpose"]
            or binding.get("profile") != receipt["profile"]
            or binding.get("authority") != receipt["authority"]
        ):
            raise ReceiptIntegrityError("receipt purpose, profile or authority mismatch")
        return receipt, binding

    def _replay_in(self, conn, receipt_id: str):
        consumed = conn.execute(
            "SELECT operation_id FROM memory_receipt_consumptions_v5 WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
        if not consumed:
            return None
        operation = conn.execute(
            "SELECT * FROM memory_operations_v3 WHERE operation_id=?",
            (consumed["operation_id"],),
        ).fetchone()
        if not operation:
            raise ReceiptIntegrityError("consumption has no authority operation")
        return self.store._operation_result(conn, operation)

    def _proposal_in(self, conn, binding: dict[str, Any]):
        proposal = conn.execute(
            "SELECT * FROM memory_core_proposals_v5 WHERE proposal_id=?",
            (binding.get("proposal_id"),),
        ).fetchone()
        if not proposal:
            raise ProposalIntegrityError("proposal not found")
        try:
            phase_context = json.loads(proposal["phase_context_json"])
            content_object = json.loads(proposal["content"])
        except (TypeError, ValueError) as exc:
            raise ProposalIntegrityError("proposal JSON is invalid") from exc
        content_digest = hashlib.sha256(proposal["content"].encode("utf-8")).hexdigest()
        phase_digest = hash_payload(phase_context)
        reason_digest = hashlib.sha256(proposal["reason"].encode("utf-8")).hexdigest()
        proposal_fields = {
            "profile": proposal["profile"],
            "record_id": proposal["record_id"],
            "base_core_revision_id": proposal["base_core_revision_id"],
            "title": proposal["title"],
            "summary": proposal["summary"],
            "content_sha256": content_digest,
            "phase_context_sha256": phase_digest,
            "reason_sha256": reason_digest,
            "source_ref": proposal["source_ref"],
        }
        proposal_digest = hash_payload(proposal_fields)
        merged = {
            "title": proposal["title"],
            "summary": proposal["summary"],
            **content_object,
        } if isinstance(content_object, dict) else {}
        if not isinstance(content_object, dict):
            raise ProposalIntegrityError("proposal content must be an object")
        errors = validate_core(merged)
        checks = (
            (content_digest, proposal["content_sha256"], "content_sha256"),
            (phase_digest, proposal["phase_context_sha256"], "phase_context_sha256"),
            (reason_digest, proposal["reason_sha256"], "reason_sha256"),
            (proposal_digest, proposal["proposal_sha256"], "proposal_sha256"),
            ("core-proposal:" + proposal_digest[:32], proposal["proposal_id"], "proposal_id"),
        )
        for actual, expected, label in checks:
            if actual != expected:
                raise ProposalIntegrityError(f"proposal {label} mismatch")
        if errors:
            raise ProposalIntegrityError("; ".join(errors))
        if hash_payload(content_object.get("phase_context")) != phase_digest:
            raise ProposalIntegrityError("phase_context in content does not match proposal")
        if proposal["profile"] != binding.get("profile"):
            raise ProposalIntegrityError("proposal profile does not match binding")
        for key in (
            "proposal_id", "proposal_sha256", "content_sha256",
            "phase_context_sha256", "reason_sha256", "source_ref",
        ):
            if binding.get(key) != proposal[key]:
                raise ProposalIntegrityError(f"binding {key} does not match proposal")
        return proposal

    def identity_core_apply(self, receipt_id: str) -> dict[str, Any]:
        self.store.require_writable()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            receipt, binding = self._receipt_in(conn, receipt_id, "identity_core_revision")
            replay = self._replay_in(conn, receipt_id)
            if replay is not None:
                return replay
            proposal = self._proposal_in(conn, binding)
            if conn.execute(
                "SELECT 1 FROM memory_proposal_decisions_v5 WHERE proposal_id=?",
                (proposal["proposal_id"],),
            ).fetchone():
                raise ProposalDecided("proposal is already decided")
            current = conn.execute(
                "SELECT * FROM memory_current_v3 WHERE record_id='core'"
            ).fetchone()
            if not current or binding.get("current_core_revision_id") != current["revision_id"]:
                raise StaleAuthority("receipt current core does not match actual current core")
            outcome = binding.get("outcome")
            if outcome not in {"apply", "reject"}:
                raise ReceiptIntegrityError("receipt outcome is invalid")
            if outcome == "apply" and proposal["base_core_revision_id"] != current["revision_id"]:
                raise StaleAuthority("proposal base does not match current core")
            key = "authority-v2:" + receipt_id
            operation_id = "operation:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
            decision_note = str(binding.get("decision_note", ""))
            revision_id = None
            evidence_ids: list[str] = []
            if outcome == "apply":
                evidence_ids = [
                    self.store._insert_evidence(conn, {
                        "evidence_type": "self_log",
                        "source_ref": proposal["source_ref"],
                        "content_summary": proposal["reason"][:300],
                        "confidence": 0.9,
                        "actor": proposal["created_by"],
                        "privacy_class": "private",
                    }),
                    self.store._insert_evidence(conn, {
                        "evidence_type": "owner_receipt",
                        "source_ref": receipt_id,
                        "content_summary": decision_note or outcome,
                        "confidence": 1.0,
                        "actor": receipt["issued_by"],
                        "privacy_class": "private",
                    }),
                ]
                next_number = int(current["revision_number"]) + 1
                revision_id = self.store._revision_id(CORE_ID, next_number, key)
            details = {
                "receipt_id": receipt_id,
                "proposal_id": proposal["proposal_id"],
                "outcome": outcome,
                "decision_note": decision_note,
            }
            if revision_id is not None:
                details["core_revision_id"] = revision_id
            operation = self.store._insert_operation(
                conn,
                operation_type="identity_core_revision",
                actor=receipt["issued_by"],
                surface=self.surface,
                record_id=CORE_ID,
                revision_id=None,
                evidence_ids=evidence_ids,
                decision="materialized",
                reason=decision_note or outcome,
                details=details,
                idempotency_key=key,
            )
            if outcome == "apply":
                materialized = self.store._revise_in(
                    conn,
                    CORE_ID,
                    actor=receipt["issued_by"],
                    reason=proposal["reason"],
                    idempotency_key=key,
                    changes={
                        "title": proposal["title"],
                        "summary": proposal["summary"],
                        "content": proposal["content"],
                    },
                    evidence_ids=evidence_ids,
                    operation_id=operation_id,
                    surface=self.surface,
                )
                if materialized != revision_id:
                    raise RuntimeError("authority revision id drift")
            conn.execute(
                "INSERT INTO memory_proposal_decisions_v5("
                "proposal_id,receipt_id,outcome,core_revision_id,decided_at"
                ") VALUES(?,?,?,?,?)",
                (
                    proposal["proposal_id"], receipt_id,
                    "applied" if outcome == "apply" else "rejected",
                    revision_id, utc_now(),
                ),
            )
            conn.execute(
                "INSERT INTO memory_receipt_consumptions_v5(receipt_id,operation_id,consumed_at) "
                "VALUES(?,?,?)",
                (receipt_id, operation_id, utc_now()),
            )
            return self.store._operation_result(conn, operation)

    def identity_retract(self, receipt_id: str) -> dict[str, Any]:
        self.store.require_writable()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            receipt, binding = self._receipt_in(conn, receipt_id, "identity_retract")
            replay = self._replay_in(conn, receipt_id)
            if replay is not None:
                return replay
            record_id = binding.get("record_id")
            if record_id in PINNED:
                raise ReceiptIntegrityError("retract receipt targets a pinned record")
            current = conn.execute(
                "SELECT * FROM memory_current_v3 WHERE record_id=?", (record_id,)
            ).fetchone()
            if not current or current["revision_id"] != binding.get("current_revision_id"):
                raise StaleAuthority("retract target revision is stale")
            reason = binding.get("reason")
            if not isinstance(reason, str) or hashlib.sha256(reason.encode("utf-8")).hexdigest() != binding.get("reason_sha256"):
                raise ReceiptIntegrityError("retract reason digest mismatch")
            key = "authority-v2:" + receipt_id
            evidence_id = self.store._insert_evidence(conn, {
                "evidence_type": "owner_receipt",
                "source_ref": receipt_id,
                "content_summary": reason,
                "confidence": 1.0,
                "actor": receipt["issued_by"],
                "privacy_class": "private",
            })
            operation = self.store._insert_operation(
                conn,
                operation_type="identity_retract",
                actor=receipt["issued_by"],
                surface=self.surface,
                record_id=record_id,
                revision_id=current["revision_id"],
                evidence_ids=[evidence_id],
                decision="materialized",
                reason=reason,
                details={"receipt_id": receipt_id, "record_id": record_id},
                idempotency_key=key,
            )
            self.store._invalidate_in(
                conn,
                record_id,
                actor=receipt["issued_by"],
                reason=reason,
                idempotency_key=key,
                evidence_ids=[evidence_id],
                operation_id=operation["operation_id"],
                surface=self.surface,
            )
            conn.execute(
                "INSERT INTO memory_receipt_consumptions_v5 VALUES(?,?,?)",
                (receipt_id, operation["operation_id"], utc_now()),
            )
            return self.store._operation_result(conn, operation)

    def identity_close_legacy_discussion(self, receipt_id: str) -> dict[str, Any]:
        self.store.require_writable()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            receipt, binding = self._receipt_in(
                conn, receipt_id, "identity_legacy_discussion_close"
            )
            replay = self._replay_in(conn, receipt_id)
            if replay is not None:
                return replay
            relation = conn.execute(
                "SELECT * FROM memory_relation_events_v4 WHERE from_record_id=? "
                "AND to_record_id=? AND relation_type='awaiting-discussion' "
                "ORDER BY sequence_number DESC LIMIT 1",
                (CORE_ID, DISCUSSION_ANCHOR),
            ).fetchone()
            current = conn.execute(
                "SELECT * FROM memory_current_v3 WHERE record_id='core'"
            ).fetchone()
            if (
                not relation
                or relation["event_type"] != "assert"
                or relation["relation_event_id"] != binding.get("relation_event_id")
                or relation["source_revision_id"] != binding.get("relation_source_revision_id")
                or not current
                or current["revision_id"] != binding.get("core_revision_id")
            ):
                raise StaleAuthority("legacy discussion binding is stale")
            note = binding.get("note")
            if not isinstance(note, str) or hashlib.sha256(note.encode("utf-8")).hexdigest() != binding.get("note_sha256"):
                raise ReceiptIntegrityError("legacy discussion note digest mismatch")
            key = "authority-v2:" + receipt_id
            relation_event_id = "relation-event:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
            operation = self.store._insert_operation(
                conn,
                operation_type="identity_legacy_discussion_close",
                actor=receipt["issued_by"],
                surface=self.surface,
                record_id=CORE_ID,
                revision_id=current["revision_id"],
                evidence_ids=[],
                decision="materialized",
                reason=note,
                details={
                    "receipt_id": receipt_id,
                    "relation_event_id": relation_event_id,
                },
                idempotency_key=key,
            )
            self.store._retract_relation_in(
                conn,
                latest=relation,
                actor=receipt["issued_by"],
                surface=self.surface,
                reason=note,
                idempotency_key=key,
            )
            conn.execute(
                "INSERT INTO memory_receipt_consumptions_v5 VALUES(?,?,?)",
                (receipt_id, operation["operation_id"], utc_now()),
            )
            return self.store._operation_result(conn, operation)

    def apply_receipt(self, receipt_id: str) -> dict[str, Any]:
        self.store.require_writable()
        with self.store.connect(readonly=True) as conn:
            receipt = conn.execute(
                "SELECT purpose FROM memory_owner_receipts_v5 WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
        if not receipt:
            raise ReceiptNotFound(f"unknown receipt {receipt_id}")
        return {
            "identity_core_revision": self.identity_core_apply,
            "identity_retract": self.identity_retract,
            "identity_legacy_discussion_close": self.identity_close_legacy_discussion,
        }[receipt["purpose"]](receipt_id)

    def close_loop(self, record_id: str, *, note: str, actor: str | None = None) -> dict[str, Any]:
        self.store.require_writable()
        return self.store.retract_relation(
            from_record_id=record_id,
            to_record_id=OPEN_LOOP_ANCHOR,
            relation_type="open-loop",
            actor=actor or self.profile.agent,
            reason=note,
            surface=self.surface,
        )

    # ------------------------------------------------------------------- read

    def retrieve(
        self,
        cue: str,
        *,
        limit: int = 10,
        token_budget: int = 2400,
        include_history: bool | None = None,
        track: bool = True,
    ) -> dict[str, Any]:
        # R2a Q1: a legacy-v4 store is fully readable. Tracking writes
        # (record_access, recall maintenance) only happen on a writable store;
        # on v4 recall degrades transparently to read-only.
        track = track and self.store.schema_info()["state"] == "ready"
        hits = self.runtime.retrieve(
            cue,
            limit=limit,
            # Select by relevance; the packet renderer enforces the real budget.
            token_budget=max(token_budget * 8, 20000),
            include_history=include_history,
            track_access=track,
            min_accessibility=self.activation.dormant_below,
            wake_relation_types=CAUSAL_RELATIONS,
            access_gain=0.0,  # gain is applied by the activation policy below
        )
        # Pinned memories (core, ontology) lead the packet so a long memory
        # never pushes them out of the budget.
        present = {hit.revision["record_id"] for hit in hits}
        for record_id in (CORE_ID, VHO_ID):
            if record_id not in present:
                rows = self.store.current_view(record_id)
                if rows:
                    hits.append(MemoryHit(rows[0], 0.0, ["pinned"]))
        hits = sorted(hits, key=lambda hit: hit.revision["record_id"] not in PINNED)
        if track:
            apply_recall(self.store, hits, self.activation, pinned=PINNED)
        packet = PacketRenderer(self.runtime.profile).render(
            cue, hits, scope="global", surface=self.surface, compact=False, token_budget=token_budget
        )
        items = []
        self_count = 0
        for hit in hits:
            revision = hit.revision
            evidence = self.store.evidence_for_revision(revision["revision_id"])
            self_authored = bool(evidence) and all(
                item.get("evidence_type") == "self_log" for item in evidence
            )
            self_count += self_authored
            items.append({
                "record_id": revision["record_id"],
                "domain": revision["domain"],
                "title": revision["title"],
                "summary": revision["summary"],
                "revision": revision["revision_number"],
                "state": state_of(revision, self.activation, PINNED),
                "self_authored": self_authored,
                "reasons": hit.reasons[:6],
                "work": self._linked_work(revision.get("content", "")),
            })
        return {
            "schema": "trajecta-identity-packet/v1",
            "profile": self.profile.name,
            "cue": cue,
            "memory_decides_truth": False,
            "open_core_proposals": self.open_core_proposals(),
            "open_discussions": self.open_discussions(),
            "open_loops": self.open_loops(),
            "causal_neighbors": self._causal_neighbors([item["record_id"] for item in items]),
            "items": items,
            "self_authored_share": round(self_count / len(items), 2) if items else 0.0,
            "packet": packet,
        }

    def open_discussions(self) -> list[dict[str, Any]]:
        rows = [
            row for row in self.store.active_relation_rows()
            if row["relation_type"] == "awaiting-discussion"
        ]
        result = []
        for row in rows:
            history = self.store.relation_history(
                from_record_id=row["from_record_id"],
                to_record_id=row["to_record_id"],
                relation_type=row["relation_type"],
            )
            result.append({
                "record_id": row["from_record_id"],
                "since": history[-1]["created_at"],
                "reason": history[-1]["reason"],
            })
        return result

    def open_loops(self) -> list[dict[str, Any]]:
        current = {row["record_id"]: row for row in self.store.current_view()}
        return [
            {"record_id": row["from_record_id"], "title": current[row["from_record_id"]]["title"]}
            for row in self.store.active_relation_rows()
            if row["relation_type"] == "open-loop" and row["from_record_id"] in current
        ]

    def timeline(self, limit: int = 20) -> list[dict[str, Any]]:
        phases = [row for row in self.store.current_view() if row["domain"] == "phase"]
        phases.sort(key=_occurred, reverse=True)
        return [
            {
                "record_id": row["record_id"],
                "at": _occurred(row),
                "title": row["title"],
                "summary": row["summary"],
                "state": state_of(row, self.activation, PINNED),
            }
            for row in phases[: max(1, min(limit, 200))]
        ]

    def status(self) -> dict[str, Any]:
        info = self.store.schema_info()
        rows = self.store.current_view() if info["state"] in {"ready", "legacy-v4"} else []
        states: dict[str, int] = {}
        domains: dict[str, int] = {}
        for row in rows:
            if row["domain"] == "anchor":
                continue
            states[state_of(row, self.activation, PINNED)] = states.get(state_of(row, self.activation, PINNED), 0) + 1
            domains[row["domain"]] = domains.get(row["domain"], 0) + 1
        return {
            "schema": "trajecta-identity-status/v1",
            "profile": self.profile.name,
            "agent": self.profile.agent,
            "db": str(self.db_path),
            "store": info["state"],
            "write_policy": "self-authored proposals; owner receipt controls canonical core",
            "records": domains,
            "activation": states,
            "open_discussions": len(self.open_discussions()) if rows else 0,
            "open_core_proposals": len(self.open_core_proposals()) if rows else 0,
            "open_loops": len(self.open_loops()) if rows else 0,
            "work_store": str(self.work.root) if self.work else None,
        }

    def _linked_work(self, content: str) -> list[dict[str, Any]]:
        linked = []
        for ref in refs_in(content):
            resolved = self.work.resolve(ref) if self.work is not None else None
            linked.append(resolved if resolved else {"ref": ref, "resolved": False})
        return linked

    def decay(self, now: str | None = None) -> dict[str, Any]:
        self.store.require_writable()
        return run_decay(self.store, self.activation, pinned=PINNED, now=now)

    # --------------------------------------------------------------- helpers

    def _submit(self, *, skip_if_exists: bool = False, falsifier: str = "", **proposal: Any) -> str:
        self.store.require_writable()
        if skip_if_exists and self.store.current_view(proposal["record_id"]):
            return "exists"
        intake = self.runtime.submit(
            actor=self.profile.agent,
            logic="the agent recorded its own process",
            truth_basis="provenance is attached",
            falsifier=falsifier,
            **proposal,
        )
        if intake["status"] not in {"materialized", "no_op"}:
            raise ValueError(f"{proposal['record_id']}: {intake['status']} ({intake['decision_reason']})")
        return intake["status"]

    def _self_evidence(self, source_ref: str, summary: str, confidence: float = 0.9) -> dict[str, Any]:
        return {
            "evidence_type": "self_log",
            "source_ref": source_ref,
            "content_summary": summary[:300],
            "confidence": confidence,
            "actor": self.profile.agent,
            "privacy_class": "private",
        }

    def _evidence(self, source_ref, summary, confidence, extra) -> list[dict[str, Any]]:
        items = [self._self_evidence(source_ref, summary, confidence)]
        for item in extra:
            outside = dict(item)
            outside.setdefault("evidence_type", "outside")
            outside.setdefault("confidence", confidence)
            items.append(outside)
        return items

    def _require_existing(self, groups: dict[str, Iterable[str]]) -> dict[str, list[str]]:
        known = {row["record_id"] for row in self.store.current_view()} if self.db_path.exists() else set()
        links: dict[str, list[str]] = {}
        missing = []
        for relation, ids in groups.items():
            for record_id in ids:
                (links.setdefault(relation, []) if record_id in known else missing).append(record_id)
        if missing:
            raise ValueError("unknown record ids: " + ", ".join(missing))
        return links

    def _link(self, record_id: str, links: dict[str, list[str]], *, reason: str) -> None:
        for relation, targets in links.items():
            for target in targets:
                self._relate(record_id, target, relation, reason=reason)

    def _relate(self, source: str, target: str, relation: str, *, reason: str) -> None:
        self.store.add_relation(
            relation_id=f"{source}->{relation}->{target}",
            from_record_id=source,
            to_record_id=target,
            relation_type=relation,
            actor=self.profile.agent,
            surface=self.surface,
            reason=reason[:300] or relation,
        )

    def _cues(self, record_id: str, cues: Iterable[str], title: str) -> None:
        for cue in [*cues, title]:
            if str(cue).strip():
                self.store.add_cue(
                    profile=self.profile.name, cue=str(cue).strip(), target_record_id=record_id
                )

    @staticmethod
    def _compose(content, decided_because, work_refs, phase_context, occurred_at) -> str:
        parts = [content.strip()] if content.strip() else []
        if decided_because:
            parts.append(f"Decided because: {decided_because}")
        refs = [ref for ref in work_refs if str(ref).strip()]
        if refs:
            parts.append("Work refs (trajecta-work-memory): " + ", ".join(refs))
        if phase_context:
            parts.append("Phase context: " + json.dumps(phase_context, ensure_ascii=False, sort_keys=True))
        if occurred_at:
            parts.append(f"Occurred at: {occurred_at}")
        return "\n".join(parts)

    def _causal_neighbors(self, record_ids: list[str]) -> list[dict[str, str]]:
        wanted = set(record_ids)
        return [
            {"from": row["from_record_id"], "relation": row["relation_type"], "to": row["to_record_id"]}
            for row in self.store.active_relation_rows()
            if row["relation_type"] in CAUSAL_RELATIONS
            and (row["from_record_id"] in wanted or row["to_record_id"] in wanted)
        ]
