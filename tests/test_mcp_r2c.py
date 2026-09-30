from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from trajecta_identity.identity import IdentityMemory
from trajecta_identity.mcp_server import (
    TOOLS,
    IdentityServer,
    loads_lossless,
    process_frame,
    serve,
    validate_tool_arguments,
    wire_dumps,
)
from trajecta_identity.profile import load_profile


def server(tmp_path: Path) -> IdentityServer:
    return IdentityServer(IdentityMemory(load_profile("example"), tmp_path / "memory.sqlite3", surface="mcp"))


def rpc_bytes(instance: IdentityServer, transcript: bytes) -> bytes:
    output = io.BytesIO()
    serve(instance, io.BytesIO(transcript), output)
    return output.getvalue()


def error(code: int, message: str, request_id=None) -> bytes:
    return (
        wire_dumps({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}).encode()
        + b"\n"
    )


def call(request_id, name: str, arguments=None) -> dict:
    params = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params}


def test_raw_framing_is_per_frame_and_processes_eof_crlf_and_whitespace(tmp_path):
    transcript = (
        b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
        b"\xff\n"
        b" \t\r\n"
        b'{"jsonrpc":"2.0","id":2,"method":"ping"}\r\n'
        b'{"jsonrpc":"2.0","id":3,"method":"ping"}'
    )
    output = rpc_bytes(server(tmp_path), transcript)
    assert output == (
        b'{"jsonrpc": "2.0", "id": 1, "result": {}}\n'
        + error(-32700, "Parse error")
        + b'{"jsonrpc": "2.0", "id": 2, "result": {}}\n'
        + b'{"jsonrpc": "2.0", "id": 3, "result": {}}\n'
    )


@pytest.mark.parametrize("payload", [b"{", b"NaN", b'"\xff"'])
def test_parse_failures_have_the_closed_error(payload, tmp_path):
    assert rpc_bytes(server(tmp_path), payload) == error(-32700, "Parse error")


@pytest.mark.parametrize("payload", [b"[]", b"1", b'"request"', b"null"])
def test_non_object_requests_are_invalid(payload, tmp_path):
    assert rpc_bytes(server(tmp_path), payload) == error(-32600, "Invalid Request")


def test_notifications_depend_only_on_id_absence_and_never_dispatch(tmp_path):
    instance = server(tmp_path)
    path = instance.memory.db_path
    notification = b'{"jsonrpc":"wrong","method":"tools/call","params":{"name":"identity_log_phase"}}\n'
    assert rpc_bytes(instance, notification) == b""
    assert not path.exists()

    with_id = b'{"jsonrpc":"2.0","id":null,"method":"notifications/foo"}\n'
    assert rpc_bytes(instance, with_id) == error(-32601, "Method not found")


@pytest.mark.parametrize("token", ["0", "-0", "9007199254740993"])
def test_integer_ids_are_echoed_from_the_original_token(token, tmp_path):
    payload = f'{{"jsonrpc":"2.0","id":{token},"method":"ping"}}'.encode()
    assert rpc_bytes(server(tmp_path), payload) == f'{{"jsonrpc": "2.0", "id": {token}, "result": {{}}}}\n'.encode()


@pytest.mark.parametrize("bad_id", ["1.0", "true", "{}", "[]"])
def test_id_domain_is_closed(bad_id, tmp_path):
    payload = f'{{"jsonrpc":"2.0","id":{bad_id},"method":"ping"}}'.encode()
    assert rpc_bytes(server(tmp_path), payload) == error(-32600, "Invalid Request")


def test_lone_surrogates_never_reach_output(tmp_path):
    in_id = br'{"jsonrpc":"2.0","id":"\ud800","method":"ping"}'
    elsewhere = br'{"jsonrpc":"2.0","id":"safe","method":"ping","x":"\udfff"}'
    assert rpc_bytes(server(tmp_path), in_id) == error(-32600, "Invalid Request")
    assert rpc_bytes(server(tmp_path), elsewhere) == error(-32600, "Invalid Request", "safe")


@pytest.mark.parametrize(
    ("payload", "code", "message"),
    [
        (b'{"id":1,"method":"ping"}', -32600, "Invalid Request"),
        (b'{"jsonrpc":"1.0","id":1,"method":"ping"}', -32600, "Invalid Request"),
        (b'{"jsonrpc":"2.0","id":1,"method":1}', -32600, "Invalid Request"),
        (b'{"jsonrpc":"2.0","id":1,"method":"ping","params":[]}', -32602, "Invalid params"),
        (b'{"jsonrpc":"2.0","id":1,"method":"missing"}', -32601, "Method not found"),
    ],
)
def test_envelope_errors_are_exact(payload, code, message, tmp_path):
    assert rpc_bytes(server(tmp_path), payload) == error(code, message, 1)


@pytest.mark.parametrize("version", ["2025-06-18", "2025-03-26", "2024-11-05"])
def test_initialize_accepts_supported_versions(version, tmp_path):
    response = server(tmp_path).handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": version}}
    )
    assert response["result"]["protocolVersion"] == version


def test_initialize_defaults_and_rejects_a_non_string_version(tmp_path):
    instance = server(tmp_path)
    defaulted = instance.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    unsupported = instance.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "future"}}
    )
    invalid = instance.handle(
        {"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {"protocolVersion": 1}}
    )
    assert defaulted["result"]["protocolVersion"] == "2025-06-18"
    assert unsupported["result"]["protocolVersion"] == "2025-06-18"
    assert invalid == {"jsonrpc": "2.0", "id": 3, "error": {"code": -32602, "message": "Invalid params"}}


@pytest.mark.parametrize(
    "params",
    [{}, {"name": 1}, {"name": "identity_status", "arguments": []}],
)
def test_tools_call_structure_fails_before_invocation(params, tmp_path):
    instance = server(tmp_path)
    instance.call_tool = Mock(side_effect=AssertionError("must not dispatch"))
    response = instance.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})
    assert response == {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "Invalid params"}}
    instance.call_tool.assert_not_called()


def test_unknown_tool_is_a_tool_error_without_store_access(tmp_path):
    instance = server(tmp_path)
    response = instance.handle(call(1, "missing", {}))
    assert response["result"] == {
        "content": [{"type": "text", "text": "ValueError: unknown tool: missing"}],
        "isError": True,
    }
    assert not instance.memory.db_path.exists()


@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        ({"cue": "x", "extra": 1}, "extra: unknown key"),
        ({}, "cue: missing required key"),
        ({"cue": 1}, "cue: expected string"),
        ({"cue": ""}, "cue: shorter than 1 characters"),
        ({"cue": "x" * 2001}, "cue: longer than 2000 characters"),
        ({"cue": "x", "limit": 0}, "limit: below minimum 1"),
        ({"cue": "x", "limit": 25}, "limit: above maximum 24"),
        ({"cue": "x", "limit": 10.0}, "limit: expected integer"),
        ({"cue": "x", "limit": "10"}, "limit: expected integer"),
        ({"cue": "x", "limit": True}, "limit: expected integer"),
    ],
)
def test_validator_reason_table_and_kind_rules(arguments, reason, tmp_path):
    instance = server(tmp_path)
    instance.memory.store.schema_info = Mock(side_effect=AssertionError("validation must precede store access"))
    response = instance.handle(call(1, "identity_retrieve", arguments))
    assert response["result"] == {
        "content": [
            {
                "type": "text",
                "text": f"ValueError: invalid arguments for identity_retrieve: {reason}",
            }
        ],
        "isError": True,
    }
    assert not instance.memory.db_path.exists()


def test_validator_recurses_and_applies_defaults_to_a_copy():
    arguments = {"cue": "memory"}
    validated = validate_tool_arguments("identity_retrieve", arguments)
    assert arguments == {"cue": "memory"}
    assert validated == {"cue": "memory", "limit": 10, "budget": 2400, "track": True}

    with pytest.raises(
        ValueError,
        match=r"vho_stack/runtime_architecture: expected string$",
    ):
        validate_tool_arguments(
            "identity_core_propose",
            {"reason": "r", "phase_context": {}, "vho_stack": {"runtime_architecture": 1}},
        )


def test_array_item_path_and_max_items_use_closed_reasons():
    with pytest.raises(ValueError, match=r"follows/1: expected string$"):
        validate_tool_arguments(
            "identity_log_phase",
            {"event_id": "ev", "title": "t", "summary": "s", "follows": ["ok", 1]},
        )
    with pytest.raises(ValueError, match=r"follows: more than 20 items$"):
        validate_tool_arguments(
            "identity_log_phase",
            {"event_id": "ev", "title": "t", "summary": "s", "follows": ["x"] * 21},
        )


def test_retrieve_annotation_and_tracking_contract():
    retrieve = next(tool for tool in TOOLS if tool["name"] == "identity_retrieve")
    assert retrieve["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    }
    assert retrieve["inputSchema"]["properties"]["track"] == {"type": "boolean", "default": True}


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("identity_status", {}),
        ("identity_retrieve", {"cue": "who", "track": True}),
        ("identity_retrieve", {"cue": "who", "track": False}),
        ("identity_timeline", {}),
        ("identity_core_proposals", {}),
    ],
)
def test_never_bootstrap_tools_leave_a_missing_store_absent(name, arguments, tmp_path):
    instance = server(tmp_path)
    response = instance.handle(call(1, name, arguments))
    assert response["result"]["isError"] is False
    assert not instance.memory.db_path.exists()


def test_mutation_self_starts_only_after_valid_arguments(tmp_path):
    invalid = server(tmp_path / "invalid")
    invalid.memory.db_path.parent.mkdir()
    invalid.memory.store.schema_info = Mock(side_effect=AssertionError("validation must precede store access"))
    response = invalid.handle(call(1, "identity_log_phase", {"event_id": "e"}))
    assert response["result"]["isError"] is True
    assert not invalid.memory.db_path.exists()

    valid = server(tmp_path / "valid")
    valid.memory.db_path.parent.mkdir()
    response = valid.handle(
        call(2, "identity_log_phase", {"event_id": "event", "title": "Title", "summary": "Summary"})
    )
    assert response["result"]["isError"] is False
    assert valid.memory.db_path.exists()


def test_subprocess_accepts_one_raw_write_and_keeps_stdout_clean(tmp_path):
    db = tmp_path / "subprocess.sqlite3"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "trajecta_identity.mcp_server",
            "--profile",
            "example",
            "--db",
            str(db),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = process.communicate(
        b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n\xff\n'
        b'{"jsonrpc":"2.0","id":2,"method":"ping"}'
    )
    assert process.returncode == 0, stderr.decode(errors="replace")
    assert stdout == (
        b'{"jsonrpc": "2.0", "id": 1, "result": {}}\n'
        + error(-32700, "Parse error")
        + b'{"jsonrpc": "2.0", "id": 2, "result": {}}\n'
    )


def test_lossless_parser_preserves_key_position_kind_and_large_integer():
    parsed = loads_lossless('{"b":1,"a":1.0,"b":9007199254740993}')
    assert list(parsed) == ["b", "a"]
    assert type(parsed["b"]) is not int
    assert isinstance(parsed["b"], int)
    assert parsed["b"] == 9007199254740993
    assert type(parsed["a"]) is float


def test_process_frame_ignores_python_whitespace(tmp_path):
    assert process_frame(server(tmp_path), "\u0085\u2028".encode()) is None
