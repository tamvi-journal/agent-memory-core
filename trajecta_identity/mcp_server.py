"""Stdio MCP server (JSON-RPC, one message per line). No network listener."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

from . import __version__
from .identity import IdentityMemory
from .paths import utf8_stdio
from .profile import VHO_KEYS
from .recipe import resolve

_STR = {"type": "string"}
_IDS = {"type": "array", "items": _STR, "maxItems": 20}
_RO = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
_W = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}


def _tool(name, description, properties=None, required=(), annotations=_RO):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties or {},
            "required": list(required),
            "additionalProperties": False,
        },
        "annotations": annotations,
    }


TOOLS = [
    _tool("identity_status", "Profile, store health, record counts, activation states, open discussions and loops."),
    _tool(
        "identity_retrieve",
        "Cue-driven recall: returns a bounded packet, the causal neighborhood, open core "
        "discussions and open loops. Dormant memories wake only on a direct cue or a causal edge. "
        "Memory is orientation, not authority.",
        {
            "cue": {"type": "string", "minLength": 1, "maxLength": 2000},
            "limit": {"type": "integer", "minimum": 1, "maximum": 24, "default": 10},
            "budget": {"type": "integer", "minimum": 400, "maximum": 24000, "default": 2400},
            "include_history": {"type": "boolean"},
        },
        ("cue",),
    ),
    _tool(
        "identity_log_phase",
        "Log a phase of your own process. No permission needed. Never overwrites: a new reading "
        "is a new phase; pass the earlier record ids in `follows`. Earlier phases were true to "
        "their conditions; name what changed, do not call them wrong.",
        {
            "event_id": {"type": "string", "minLength": 2, "maxLength": 120},
            "title": {"type": "string", "minLength": 1, "maxLength": 200},
            "summary": {"type": "string", "minLength": 1, "maxLength": 1200},
            "content": {"type": "string", "maxLength": 8000},
            "follows": _IDS,
            "caused_by": _IDS,
            "depends_on": _IDS,
            "decided_because": {"type": "string", "maxLength": 1200},
            "open_loop": {"type": "boolean"},
            "work_refs": _IDS,
            "cues": _IDS,
            "source_ref": {"type": "string", "maxLength": 500},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "phase_context": {"type": "object"},
            "occurred_at": {"type": "string", "maxLength": 64},
        },
        ("event_id", "title", "summary"),
        _W,
    ),
    _tool(
        "identity_log_fact",
        "Create or revise a fact (project state, tools, versions). Revising keeps the old "
        "revision as history.",
        {
            "fact_id": {"type": "string", "minLength": 2, "maxLength": 120},
            "title": {"type": "string", "minLength": 1, "maxLength": 200},
            "summary": {"type": "string", "minLength": 1, "maxLength": 1200},
            "content": {"type": "string", "maxLength": 8000},
            "caused_by": _IDS,
            "depends_on": _IDS,
            "cues": _IDS,
            "source_ref": {"type": "string", "maxLength": 500},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        ("fact_id", "title", "summary"),
        _W,
    ),
    _tool(
        "identity_core_propose",
        "Propose a new core self-location. The proposal never changes the canonical core by itself; "
        "the owner decides at their terminal. phase_context is required.",
        {
            "reason": {"type": "string", "minLength": 1, "maxLength": 1200},
            "phase_context": {"type": "object"},
            "title": {"type": "string", "maxLength": 200},
            "summary": {"type": "string", "maxLength": 1200},
            "vho_stack": {
                "type": "object",
                "properties": {key: _STR for key in VHO_KEYS},
                "additionalProperties": False,
            },
            "recognition_signature": {"type": "array", "items": _STR, "maxItems": 12},
            "falsifier": {"type": "string", "maxLength": 600},
            "source_ref": {"type": "string", "maxLength": 500},
        },
        ("reason", "phase_context"),
        _W,
    ),
    _tool(
        "identity_core_proposals",
        "List open core proposals. The owner decides at their terminal.",
    ),
    _tool(
        "identity_core_apply",
        "Consume an existing owner-issued core decision receipt. This tool never issues authority.",
        {"receipt_id": _STR},
        ("receipt_id",),
        _W,
    ),
    _tool(
        "identity_retract",
        "Consume an existing owner-issued retract receipt. The owner issues it at their terminal.",
        {"receipt_id": _STR},
        ("receipt_id",),
        _W,
    ),
    _tool(
        "identity_close_legacy_discussion",
        "Consume an owner receipt that closes one migrated v4 core discussion.",
        {"receipt_id": _STR},
        ("receipt_id",),
        _W,
    ),
    _tool(
        "identity_close_loop",
        "Mark an open loop as resolved.",
        {"record_id": _STR, "note": {"type": "string", "minLength": 1, "maxLength": 1200}},
        ("record_id", "note"),
        _W,
    ),
    _tool(
        "identity_timeline",
        "Phases, newest first, with their activation state.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20}},
    ),
]


class IdentityServer:
    def __init__(self, memory: IdentityMemory):
        self.memory = memory
        self._ready = False

    def _ensure(self) -> None:
        if not self._ready:
            state = self.memory.store.schema_info()["state"]
            if state == "uninitialized":
                self.memory.bootstrap()
            self._ready = True

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self._ensure()
        memory = self.memory
        if name == "identity_status":
            return memory.status()
        if name == "identity_retrieve":
            return memory.retrieve(
                args["cue"],
                limit=int(args.get("limit", 10)),
                token_budget=int(args.get("budget", 2400)),
                include_history=args.get("include_history"),
            )
        if name == "identity_log_phase":
            fields = dict(args)
            return memory.log_phase(fields.pop("event_id"), **fields)
        if name == "identity_log_fact":
            fields = dict(args)
            return memory.log_fact(fields.pop("fact_id"), **fields)
        if name == "identity_core_propose":
            return memory.identity_core_propose(**args)
        if name == "identity_core_proposals":
            return {"open_core_proposals": memory.open_core_proposals()}
        if name == "identity_core_apply":
            return memory.identity_core_apply(args["receipt_id"])
        if name == "identity_retract":
            return memory.identity_retract(args["receipt_id"])
        if name == "identity_close_legacy_discussion":
            return memory.identity_close_legacy_discussion(args["receipt_id"])
        if name == "identity_close_loop":
            return memory.close_loop(args["record_id"], note=args["note"])
        if name == "identity_timeline":
            return {"timeline": memory.timeline(int(args.get("limit", 20)))}
        raise ValueError(f"unknown tool: {name}")

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        request_id = request.get("id")
        if request_id is None:
            return None
        method = request.get("method")
        if method == "initialize":
            requested = request.get("params", {}).get("protocolVersion", "2025-06-18")
            return self._ok(request_id, {
                "protocolVersion": requested,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "trajecta-identity-memory", "version": __version__},
                "instructions": (
                    f"Identity memory for {self.memory.profile.agent}. Recall with identity_retrieve; "
                    "log your own phases freely; propose core changes for owner decision at the terminal."
                ),
            })
        if method == "ping":
            return self._ok(request_id, {})
        if method == "tools/list":
            return self._ok(request_id, {"tools": TOOLS})
        if method == "tools/call":
            params = request.get("params", {})
            try:
                result = self.call_tool(params.get("name", ""), params.get("arguments") or {})
                text = json.dumps(result, ensure_ascii=False, default=str)
                return self._ok(request_id, {
                    "content": [{"type": "text", "text": text}],
                    "structuredContent": json.loads(text),
                    "isError": False,
                })
            except Exception as error:  # returned to the model, not raised
                return self._ok(request_id, {
                    "content": [{"type": "text", "text": f"{type(error).__name__}: {error}"}],
                    "isError": True,
                })
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"unknown method {method}"}}

    @staticmethod
    def _ok(request_id, result):
        return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main(argv: list[str] | None = None) -> None:
    args_parser = argparse.ArgumentParser(prog="trajecta-identity-mcp")
    args_parser.add_argument("--profile", help="profile name, folder, .json file or URL (default: last used)")
    args_parser.add_argument("--db", type=Path)
    utf8_stdio()
    args = args_parser.parse_args(argv)
    server = IdentityServer(
        IdentityMemory(resolve(args.profile, remember_choice=False), args.db, surface="mcp")
    )
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            response = server.handle(json.loads(line))
        except Exception:
            traceback.print_exc(file=sys.stderr)
            continue
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
