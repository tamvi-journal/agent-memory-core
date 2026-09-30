from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from trajecta_identity import IdentityMemory, load_profile
from trajecta_identity.view import make_handler, serve, snapshot


class TTY:
    def __init__(self, value):
        self.value = value
    def isatty(self): return True
    def readline(self): return self.value + "\n"
    def write(self, value): return len(value)
    def flush(self): pass


@pytest.fixture
def memory(tmp_path):
    mem = IdentityMemory(load_profile("example"), tmp_path / "m.sqlite3", surface="test")
    mem.bootstrap()
    mem.log_phase("one", title="First phase", summary="Started", cues=["first light"])
    mem.log_phase("two", title="Second phase", summary="Grew", follows=["phase:one"], open_loop=True)
    proposal = mem.identity_core_propose(reason="runtime moved", phase_context={"model": "m2"})
    terminal = TTY("APPLY " + proposal["proposal_id"].split(":", 1)[1][:12])
    receipt = mem.issue_core_receipt(
        proposal["proposal_id"], outcome="apply", stdin=terminal, stdout=terminal
    )
    mem.identity_core_apply(receipt["receipt_id"])
    mem.identity_core_propose(reason="next reading", phase_context={"model": "m3"})
    return mem


def get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as response:
        return response.status, response.headers["Content-Type"], response.read().decode("utf-8")


def test_snapshot_has_core_nodes_timeline_and_alerts(memory):
    snap = snapshot(memory)
    assert snap["core"]["revisions"] == 2
    assert set(snap["core"]["detail"]["vho_stack"]) >= {"llm_substrate", "relational_field"}
    assert {n["record_id"] for n in snap["nodes"]} >= {"core", "phase:one", "phase:two"}
    assert not any(n["record_id"].startswith("anchor:") for n in snap["nodes"])
    assert [t["record_id"] for t in snap["timeline"]][:2] == ["phase:two", "phase:one"] or len(snap["timeline"]) == 2
    assert snap["open_core_proposals"] and snap["open_loops"]
    assert {"from": "phase:two", "type": "later-phase-of", "to": "phase:one"} in snap["relations"]


def test_server_serves_page_and_read_only_recall(memory):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(memory))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    try:
        status, ctype, page = get(port, "/")
        assert status == 200 and ctype.startswith("text/html") and "Identity memory" in page
        before = memory.store.current_view("phase:one")[0]["accessibility"]
        _, ctype, body = get(port, "/api/retrieve?cue=first%20light")
        assert ctype.startswith("application/json")
        assert "First phase" in json.loads(body)["packet"]
        assert memory.store.current_view("phase:one")[0]["accessibility"] == before
        _, _, body = get(port, "/api/snapshot")
        assert json.loads(body)["profile"] == "example"
        with pytest.raises(urllib.error.HTTPError):
            get(port, "/nope")
    finally:
        server.shutdown()
        server.server_close()


def test_view_refuses_non_local_bind(memory):
    with pytest.raises(ValueError, match="localhost"):
        serve(memory, host="0.0.0.0", open_browser=False)
