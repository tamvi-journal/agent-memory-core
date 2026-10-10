"""Stdio MCP server with the frozen R2c JSON-RPC byte boundary."""

from __future__ import annotations

import argparse
import copy
import json
import math
import sys
import traceback
from pathlib import Path
from typing import Any, BinaryIO, Iterator

from . import __version__
from .identity import IdentityMemory
from .paths import utf8_stdio
from .profile import VHO_KEYS
from .recipe import resolve

SUPPORTED = ["2025-06-18", "2025-03-26", "2024-11-05"]
NEVER_BOOTSTRAP = {
    "identity_status",
    "identity_retrieve",
    "identity_timeline",
    "identity_core_proposals",
}

_STR = {"type": "string"}
_IDS = {"type": "array", "items": _STR, "maxItems": 20}
_RO = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
_W = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
_RETRIEVE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}


class IntToken(int):
    """An integer that retains its original JSON token for request-id echoing."""

    raw: str

    def __new__(cls, raw: str):
        value = super().__new__(cls, raw)
        value.raw = raw
        return value


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
        "Memory is orientation, not authority. Recall may update activation; pass track=false for a pure read.",
        {
            "cue": {"type": "string", "minLength": 1, "maxLength": 2000},
            "limit": {"type": "integer", "minimum": 1, "maximum": 24, "default": 10},
            "budget": {"type": "integer", "minimum": 400, "maximum": 24000, "default": 2400},
            "include_history": {"type": "boolean"},
            "track": {"type": "boolean", "default": True},
        },
        ("cue",),
        _RETRIEVE,
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
    _tool("identity_core_proposals", "List open core proposals. The owner decides at their terminal."),
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

_TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "null"


def _join_path(path: str, member: str | int) -> str:
    return f"{path}/{member}" if path else str(member)


def _validation_error(path: str, reason: str) -> None:
    raise ValueError(f"{path}: {reason}")


def _validate(value: Any, schema: dict[str, Any], path: str) -> None:
    expected = schema.get("type")
    actual = _kind(value)
    valid_type = actual in {"integer", "number"} if expected == "number" else actual == expected
    if expected is not None and not valid_type:
        _validation_error(path, f"expected {expected}")

    if expected == "object":
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    _validation_error(_join_path(path, key), "unknown key")
        for key in schema.get("required", []):
            if key not in value:
                _validation_error(_join_path(path, key), "missing required key")
        for key, child_schema in properties.items():
            if key in value:
                _validate(value[key], child_schema, _join_path(path, key))
        return

    if expected == "string":
        if "minLength" in schema and len(value) < schema["minLength"]:
            _validation_error(path, f"shorter than {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            _validation_error(path, f"longer than {schema['maxLength']} characters")
        return

    if expected in {"integer", "number"}:
        if "minimum" in schema and value < schema["minimum"]:
            _validation_error(path, f"below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            _validation_error(path, f"above maximum {schema['maximum']}")
        return

    if expected == "array":
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            _validation_error(path, f"more than {schema['maxItems']} items")
        if "items" in schema:
            for index, item in enumerate(value):
                _validate(item, schema["items"], _join_path(path, index))


def _copy_with_defaults(value: Any, schema: dict[str, Any]) -> Any:
    if schema.get("type") == "object":
        result = copy.deepcopy(value)
        for key, child_schema in schema.get("properties", {}).items():
            if key in result:
                result[key] = _copy_with_defaults(result[key], child_schema)
            elif "default" in child_schema:
                result[key] = copy.deepcopy(child_schema["default"])
        return result
    if schema.get("type") == "array" and "items" in schema:
        return [_copy_with_defaults(item, schema["items"]) for item in value]
    return copy.deepcopy(value)


def validate_tool_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    tool = _TOOLS_BY_NAME.get(name)
    if tool is None:
        return copy.deepcopy(arguments)
    schema = tool["inputSchema"]
    try:
        _validate(arguments, schema, "")
    except ValueError as error:
        raise ValueError(f"invalid arguments for {name}: {error}") from None
    return _copy_with_defaults(arguments, schema)


def _contains_lone_surrogate(value: Any) -> bool:
    if isinstance(value, str):
        return any(0xD800 <= ord(character) <= 0xDFFF for character in value)
    if isinstance(value, list):
        return any(_contains_lone_surrogate(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_lone_surrogate(key) or _contains_lone_surrogate(item) for key, item in value.items())
    return False


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def _finite_float(token: str) -> float:
    """R0 §4.1: NaN and Infinity are never accepted. CPython json turns an
    overflowing token such as 1e400 into inf; the frozen contract (and the TS
    parser) treats that frame as a parse error instead."""
    value = float(token)
    if not math.isfinite(value):
        raise ValueError(f"non-finite number {token}")
    return value


def loads_lossless(source: str) -> Any:
    return json.loads(
        source, parse_int=IntToken, parse_float=_finite_float, parse_constant=_reject_constant
    )


def wire_dumps(value: Any) -> str:
    """Match json.dumps defaults while retaining IntToken spelling."""

    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, IntToken):
        return value.raw
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(wire_dumps(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(
            f"{json.dumps(key, ensure_ascii=False)}: {wire_dumps(item)}" for key, item in value.items()
        ) + "}"
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


class IdentityServer:
    def __init__(self, memory: IdentityMemory):
        self.memory = memory

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        args = validate_tool_arguments(name, args)
        memory = self.memory
        if name not in NEVER_BOOTSTRAP and name in _TOOLS_BY_NAME:
            if memory.store.schema_info()["state"] == "uninitialized":
                memory.bootstrap()
        if name == "identity_status":
            return memory.status()
        if name == "identity_retrieve":
            return memory.retrieve(
                args["cue"],
                limit=int(args["limit"]),
                token_budget=int(args["budget"]),
                include_history=args.get("include_history"),
                track=args["track"],
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
            return {"timeline": memory.timeline(int(args["limit"]))}
        raise ValueError(f"unknown tool: {name}")

    def handle(self, request: Any) -> dict[str, Any] | None:
        if not isinstance(request, dict):
            return self._error(None, -32600, "Invalid Request")
        if "id" not in request:
            return None

        request_id = request["id"]
        if not self._valid_id(request_id):
            return self._error(None, -32600, "Invalid Request")
        if _contains_lone_surrogate(request):
            safe_id = None if _contains_lone_surrogate(request_id) else request_id
            return self._error(safe_id, -32600, "Invalid Request")
        if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return self._error(request_id, -32600, "Invalid Request")
        if "params" in request and not isinstance(request["params"], dict):
            return self._error(request_id, -32602, "Invalid params")

        method = request["method"]
        params = request.get("params", {})
        if method == "initialize":
            requested = params.get("protocolVersion", SUPPORTED[0])
            if not isinstance(requested, str):
                return self._error(request_id, -32602, "Invalid params")
            protocol = requested if requested in SUPPORTED else SUPPORTED[0]
            return self._ok(
                request_id,
                {
                    "protocolVersion": protocol,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "trajecta-identity-memory", "version": __version__},
                    "instructions": (
                        f"Identity memory for {self.memory.profile.agent}. Recall with identity_retrieve; "
                        "log your own phases freely; propose core changes for owner decision at the terminal."
                    ),
                },
            )
        if method == "ping":
            return self._ok(request_id, {})
        if method == "tools/list":
            return self._ok(request_id, {"tools": TOOLS})
        if method == "tools/call":
            if "name" not in params or not isinstance(params["name"], str):
                return self._error(request_id, -32602, "Invalid params")
            if "arguments" in params and not isinstance(params["arguments"], dict):
                return self._error(request_id, -32602, "Invalid params")
            try:
                result = self.call_tool(params["name"], params.get("arguments", {}))
                text = json.dumps(result, ensure_ascii=False, default=str)
                return self._ok(
                    request_id,
                    {
                        "content": [{"type": "text", "text": text}],
                        "structuredContent": loads_lossless(text),
                        "isError": False,
                    },
                )
            except Exception as error:  # returned to the model, not raised
                return self._ok(request_id, self._tool_error(error))
        return self._error(request_id, -32601, "Method not found")

    @staticmethod
    def _valid_id(value: Any) -> bool:
        return value is None or isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))

    @staticmethod
    def _tool_error(error: Exception) -> dict[str, Any]:
        public_names = {
            "FileExistsError",
            "IncompatibleJournalMode",
            "MigrationRequiredError",
            "PinnedRecordError",
            "ProposalDecided",
            "ProposalIntegrityError",
            "ReceiptIntegrityError",
            "ReceiptNotFound",
            "RuntimeError",
            "SchemaVersionError",
            "StoreBusy",
            "StaleAuthority",
            "ValueError",
            "WorkStoreError",
        }
        name = type(error).__name__
        if name not in public_names:
            traceback.print_exception(error, file=sys.stderr)
            text = "RuntimeError: internal error"
        else:
            text = f"{name}: {error}"
        return {"content": [{"type": "text", "text": text}], "isError": True}

    @staticmethod
    def _ok(request_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def iter_frames(stream: BinaryIO) -> Iterator[bytes]:
    pending = bytearray()
    while True:
        read = getattr(stream, "read1", stream.read)
        chunk = read(65536)
        if not chunk:
            break
        pending.extend(chunk)
        while True:
            newline = pending.find(b"\n")
            if newline < 0:
                break
            frame = bytes(pending[:newline])
            del pending[: newline + 1]
            yield frame
    if pending:
        yield bytes(pending)


def process_frame(server: IdentityServer, frame: bytes) -> dict[str, Any] | None:
    if frame.endswith(b"\r"):
        frame = frame[:-1]
    try:
        source = frame.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return server._error(None, -32700, "Parse error")
    if not source.strip():
        return None
    try:
        request = loads_lossless(source)
    except (ValueError, TypeError, json.JSONDecodeError):
        return server._error(None, -32700, "Parse error")
    return server.handle(request)


def serve(server: IdentityServer, stdin: BinaryIO, stdout: BinaryIO) -> None:
    for frame in iter_frames(stdin):
        response = process_frame(server, frame)
        if response is not None:
            stdout.write(wire_dumps(response).encode("utf-8") + b"\n")
            stdout.flush()


def main(argv: list[str] | None = None) -> None:
    args_parser = argparse.ArgumentParser(prog="trajecta-identity-mcp")
    args_parser.add_argument("--profile", help="profile name, folder, .json file or URL (default: last used)")
    args_parser.add_argument("--db", type=Path)
    utf8_stdio()
    args = args_parser.parse_args(argv)
    server = IdentityServer(IdentityMemory(resolve(args.profile, remember_choice=False), args.db, surface="mcp"))
    serve(server, sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    main()
