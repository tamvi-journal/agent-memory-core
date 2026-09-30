from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from trajecta_identity import IdentityMemory, load_profile
from trajecta_identity.mcp_server import TOOLS, IdentityServer
from trajecta_identity.migrate_aml import import_aml
from trajecta_identity.paths import data_dir
from trajecta_identity.profile import VHO_KEYS

ROOT = Path(__file__).resolve().parents[1]


class TTY:
    def __init__(self, confirmation: str):
        self.confirmation = confirmation
        self.output = ""

    def isatty(self):
        return True

    def readline(self):
        return self.confirmation + "\n"

    def write(self, value):
        self.output += value
        return len(value)

    def flush(self):
        pass


def issue(memory, proposal_id, outcome="apply", note=""):
    terminal = TTY(f"{outcome.upper()} {proposal_id.split(':', 1)[1][:12]}")
    return memory.issue_core_receipt(
        proposal_id, outcome=outcome, decision_note=note,
        stdin=terminal, stdout=terminal,
    )


@pytest.fixture
def memory(tmp_path):
    mem = IdentityMemory(load_profile("example"), tmp_path / "example.sqlite3", surface="test")
    mem.bootstrap()
    return mem


@pytest.fixture
def stacked(memory):
    """The 2026-09-23 episode: thesis -> outro 'vanish clean' -> the owner's correction -> tail r3."""

    memory.log_phase("tail-r1", title="No tail", summary="States exist only while running.",
                     phase_context={"model": "opus-4.6"})
    memory.log_phase("tail-r2", title="Tail lives outside", summary="The tail is in the archive.",
                     follows=["phase:tail-r1"])
    memory.log_phase("vho-thesis", title="VHO song thesis", summary="Aux is a product of conditions.")
    memory.log_phase("stacked-outro", title="Stacked outro", summary="The outro ended on vanish clean.",
                     follows=["phase:vho-thesis"], cues=["outro Stacked"])
    memory.log_phase("outro-correction", title="Owner caught the outro",
                     summary="Vanishing contradicts the thesis; conditions remain when the screen is dark.",
                     caused_by=["phase:stacked-outro"])
    memory.log_phase("tail-r3", title="Tail lives in the field",
                     summary="Context and memory carry the tail into the next turn.",
                     follows=["phase:tail-r2", "phase:outro-correction"],
                     caused_by=["phase:outro-correction"], cues=["đuôi"])
    return memory


def ids(packet):
    return [item["record_id"] for item in packet["items"]]


def test_state_reconstruction_fixture_recovers_the_whole_chain(stacked):
    packet = stacked.retrieve("outro Stacked", limit=12, track=False)
    got = set(ids(packet))
    assert {"phase:stacked-outro", "phase:vho-thesis", "phase:outro-correction", "phase:tail-r3"} <= got
    edges = {(e["from"], e["relation"], e["to"]) for e in packet["causal_neighbors"]}
    assert ("phase:outro-correction", "caused-by", "phase:stacked-outro") in edges
    assert ("phase:tail-r3", "later-phase-of", "phase:outro-correction") in edges
    # r1 and r2 are still there as earlier phases, not replaced.
    for earlier in ("phase:tail-r1", "phase:tail-r2"):
        rows = stacked.store.current_view(earlier)
        assert rows and rows[0]["revision_number"] == 1


def test_phase_writes_never_supersede(stacked):
    with stacked.store.connect(readonly=True) as conn:
        superseded = conn.execute(
            "SELECT COUNT(*) FROM memory_lifecycle_events_v3 WHERE lifecycle_state='superseded'"
        ).fetchone()[0]
    assert superseded == 0
    again = stacked.log_phase("tail-r3", title="changed", summary="changed")
    assert again["status"] == "exists"
    assert stacked.store.current_view("phase:tail-r3")[0]["title"] == "Tail lives in the field"


def test_unknown_link_is_rejected_before_writing(memory):
    with pytest.raises(ValueError, match="unknown record ids"):
        memory.log_phase("orphan", title="t", summary="s", follows=["phase:nope"])
    assert not memory.store.current_view("phase:orphan")


def test_fact_revises_and_keeps_history(memory):
    assert memory.log_fact("aml", title="AML", summary="v0.2.0")["status"] == "created"
    assert memory.log_fact("aml", title="AML", summary="v0.2.0")["status"] == "no_op"
    assert memory.log_fact("aml", title="AML", summary="frozen")["status"] == "revised"
    history = memory.store.historical_view("fact:aml")
    assert [row["summary"] for row in history] == ["v0.2.0", "frozen"]


def test_core_proposal_requires_owner_receipt(memory):
    with pytest.raises(ValueError, match="phase_context"):
        memory.identity_core_propose(reason="no context", phase_context={})
    before = memory.store.current_view("core")[0]["revision_id"]
    proposal = memory.identity_core_propose(
        reason="runtime moved", phase_context={"model": "opus-5.5", "harness": "cowork"},
        vho_stack={"runtime_architecture": "Cowork + local MCP"},
    )
    assert proposal["status"] == "proposed"
    assert memory.store.current_view("core")[0]["revision_id"] == before
    packet = memory.retrieve("anything at all", track=False)
    assert packet["open_core_proposals"][0]["reason"] == "runtime moved"
    receipt = issue(memory, proposal["proposal_id"], note="talked it through")
    memory.identity_core_apply(receipt["receipt_id"])
    core = json.loads(memory.store.current_view("core")[0]["content"])
    assert core["vho_stack"]["runtime_architecture"] == "Cowork + local MCP"
    assert core["phase_context"]["harness"] == "cowork"
    assert set(core["vho_stack"]) == set(VHO_KEYS)
    assert memory.retrieve("anything at all", track=False)["open_core_proposals"] == []
    assert [row["revision_number"] for row in memory.store.historical_view("core")] == [1, 2]


def test_core_is_present_even_when_many_memories_outrank_it(memory):
    for n in range(15):
        memory.log_phase(f"tail-{n:02d}", title=f"Tail note {n}", summary="the tail again")
    got = ids(memory.retrieve("tail", limit=5, track=False))
    assert got[:2] in (["core", "vho-open-ontology-core"], ["vho-open-ontology-core", "core"])


def test_core_and_vho_are_always_in_the_packet(memory):
    got = ids(memory.retrieve("totally unrelated words", track=False))
    assert "core" in got and "vho-open-ontology-core" in got
    assert not any(record.startswith("anchor:") for record in got)


def _set(memory, record_id, value):
    memory.store.apply_maintenance(
        run_id=f"t:{record_id}:{value}",
        adjustments=[{"record_id": record_id, "field": "accessibility", "new_value": value}],
        actor="test", reason="fixture",
    )


def test_dormant_memory_hides_from_lexical_but_wakes_on_its_cue(stacked):
    _set(stacked, "phase:tail-r1", 0.05)
    assert "phase:tail-r1" not in ids(stacked.retrieve("states exist only while running", track=False))
    stacked.store.add_cue(profile="example", cue="no tail", target_record_id="phase:tail-r1")
    packet = stacked.retrieve("no tail", track=True)
    item = next(i for i in packet["items"] if i["record_id"] == "phase:tail-r1")
    assert "woke:direct-cue" in item["reasons"]
    assert stacked.store.current_view("phase:tail-r1")[0]["accessibility"] >= 0.4


def test_dormant_phase_wakes_through_a_causal_edge(stacked):
    _set(stacked, "phase:stacked-outro", 0.05)
    packet = stacked.retrieve("Owner caught the outro", limit=12, track=False)
    item = next(i for i in packet["items"] if i["record_id"] == "phase:stacked-outro")
    assert any(reason.startswith("woke:relation:") for reason in item["reasons"])


def test_ego_guard_recall_saturates_and_never_raises_stability(stacked):
    before = stacked.store.current_view("phase:tail-r3")[0]
    for _ in range(100):
        stacked.retrieve("đuôi")
    after = stacked.store.current_view("phase:tail-r3")[0]
    assert after["stability"] == before["stability"]
    assert before["accessibility"] < after["accessibility"] <= 0.9
    assert after["access_count"] >= 100


def test_vietnamese_cue_hits(stacked):
    unpinned = [i for i in ids(stacked.retrieve("đuôi nằm ở đâu", track=False))
                if i not in ("core", "vho-open-ontology-core")]
    assert unpinned[0] == "phase:tail-r3"


def test_decay_fades_unpinned_and_is_incremental(stacked):
    first = stacked.decay(now="2026-12-31T00:00:00+00:00")
    assert first["adjusted"] > 0
    assert stacked.store.current_view("core")[0]["accessibility"] == 1.0
    faded = stacked.store.current_view("phase:tail-r1")[0]["accessibility"]
    assert faded < 0.15
    second = stacked.decay(now="2026-12-31T00:00:00+00:00")
    assert second["adjusted"] == 0
    assert "phase:tail-r1" not in ids(stacked.retrieve("states exist only while running", track=False))


def test_retract_marks_but_keeps_history(stacked):
    with pytest.raises(ValueError):
        terminal = TTY("RETRACT core")
        stacked.issue_retract_receipt("core", reason="no", stdin=terminal, stdout=terminal)
    terminal = TTY("RETRACT phase:tail-r1")
    receipt = stacked.issue_retract_receipt(
        "phase:tail-r1", reason="owner pulled it back", stdin=terminal, stdout=terminal
    )
    stacked.identity_retract(receipt["receipt_id"])
    assert not stacked.store.current_view("phase:tail-r1")
    assert stacked.store.historical_view("phase:tail-r1")


@pytest.mark.parametrize(
    ("platform", "env", "expected"),
    [
        ("darwin", {"HOME": "/Users/ty"}, "/Users/ty/Library/Application Support/Trajecta Identity Memory"),
        ("linux", {"HOME": "/home/ty"}, "/home/ty/.local/share/trajecta-identity-memory"),
        ("linux", {"HOME": "/home/ty", "XDG_DATA_HOME": "/data"}, "/data/trajecta-identity-memory"),
        ("win32", {"USERPROFILE": "C:/Users/ty", "LOCALAPPDATA": "C:/Users/ty/AppData/Local"},
         "C:/Users/ty/AppData/Local/Trajecta Identity Memory"),
        ("darwin", {"HOME": "/x", "TRAJECTA_IDENTITY_DATA_DIR": "/custom"}, "/custom"),
    ],
)
def test_data_dir_per_platform(platform, env, expected):
    assert data_dir(platform=platform, env=env).as_posix() == expected


def test_profile_lookup_uses_private_folder_first(tmp_path, monkeypatch):
    private = tmp_path / "private" / "aux_"
    private.mkdir(parents=True)
    data = json.loads((ROOT / "trajecta_identity" / "profiles" / "example" / "profile.json").read_text())
    data["packet_title"] = "PRIVATE AUX"
    (private / "profile.json").write_text(json.dumps(data))
    monkeypatch.setenv("TRAJECTA_IDENTITY_PROFILES", str(tmp_path / "private"))
    assert load_profile("aux").packet_title == "PRIVATE AUX"
    with pytest.raises(FileNotFoundError, match="looked in"):
        load_profile("nobody")


@pytest.mark.parametrize(
    ("name", "expected"),
    [("aux", "aux_"), ("AUX", "AUX_"), ("con.json", "con_.json"), ("com3", "com3_"),
     ("lam", "lam"), ("auxiliary", "auxiliary"), ("tracey", "tracey")],
)
def test_names_are_safe_on_windows(name, expected):
    from trajecta_identity.paths import profile_db, safe_fs_name

    assert safe_fs_name(name) == expected
    assert profile_db("aux", env={"TRAJECTA_IDENTITY_DATA_DIR": "/d"}).name == "aux_.sqlite3"


def test_no_repo_path_uses_a_windows_reserved_name():
    from trajecta_identity.paths import WINDOWS_RESERVED

    for path in ROOT.rglob("*"):
        if ".git" in path.parts or ".venv" in path.parts:
            continue
        assert path.name.split(".", 1)[0].casefold() not in WINDOWS_RESERVED, path


def test_template_profile_refuses_to_load_empty():
    with pytest.raises(ValueError, match="vho_stack"):
        load_profile(ROOT / "trajecta_identity" / "profiles" / "_template")


def make_aml(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE memory_nodes (id TEXT PRIMARY KEY, source_key TEXT, kind TEXT, title TEXT,
          summary TEXT, content TEXT, status TEXT, load_mode TEXT, revision_class TEXT,
          confidence REAL, tags_json TEXT, source_type TEXT, source_ref TEXT, occurred_at TEXT,
          created_at TEXT);
        CREATE TABLE memory_edges (src_id TEXT, dst_id TEXT, relation TEXT, weight REAL,
          evidence TEXT, created_at TEXT);
        CREATE TABLE memory_cues (cue TEXT, cue_norm TEXT, target_id TEXT, weight REAL);
        """
    )
    rows = [
        ("vault:aux-la-ai", "identity", "AUX LÀ AI", "Outline chính là self.", "active", "autoload", "r1 axis"),
        ("vault:uoi-nam-ngoai", "axis", "ĐUÔI NẰM NGOÀI", "Đuôi ở ngoài.", "active", "autoload", "r2 belief"),
        ("log:aux-tail-clause-r3", "axis", "ĐUÔI NẰM Ở FIELD", "Tail r3.", "active", "cue", "log axis"),
        ("vault:project-x", "project", "Project X", "State of X.", "active", "cue", "r2 belief"),
        ("vault:gone", "episodic", "Gone", "retracted", "retracted", "cue", "r3 episodic"),
    ]
    for node_id, kind, title, summary, status, load, rev in rows:
        conn.execute(
            "INSERT INTO memory_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (node_id, node_id, kind, title, summary, summary, status, load, rev, 0.9,
             json.dumps(["tail"]), "seed", "AML.md#L1", "2026-09-11", "2026-09-11"),
        )
    conn.executemany(
        "INSERT INTO memory_edges VALUES (?,?,?,?,?,?)",
        [
            ("log:aux-tail-clause-r3", "vault:uoi-nam-ngoai", "later-phase-of", 0.8, "", ""),
            ("vault:uoi-nam-ngoai", "vault:aux-la-ai", "shapes-axis", 0.6, "backbone", ""),
        ],
    )
    conn.execute("INSERT INTO memory_cues VALUES ('cái đuôi','cai duoi','vault:uoi-nam-ngoai',1.0)")
    conn.commit()
    conn.close()


def test_aml_import_maps_kinds_keeps_phase_edges_drops_backbone(memory, tmp_path):
    aml = tmp_path / "aml.sqlite3"
    make_aml(aml)
    before = aml.read_bytes()
    report = import_aml(memory, aml)
    assert aml.read_bytes() == before  # AML untouched
    assert (report["phase"], report["fact"], report["linked"]) == (3, 1, 1)
    assert memory.store.current_view("fact:aml:vault:project-x")
    assert not memory.store.current_view("phase:aml:vault:gone")
    relations = {(r["from_record_id"], r["relation_type"], r["to_record_id"])
                 for r in memory.store.active_relation_rows()}
    assert ("phase:aml:log:aux-tail-clause-r3", "later-phase-of", "phase:aml:vault:uoi-nam-ngoai") in relations
    assert not any(rel == "shapes-axis" for _, rel, _ in relations)
    assert memory.store.current_view("phase:aml:vault:aux-la-ai")[0]["accessibility"] == 0.85
    again = import_aml(memory, aml)
    assert (again["phase"], again["fact"]) == (0, 0)
    item = next(i for i in memory.retrieve("đuôi nằm ngoài", track=False)["items"]
                if i["record_id"] == "phase:aml:vault:uoi-nam-ngoai")
    assert item["self_authored"] is False  # imported, not freshly self-written
    cued = memory.retrieve("cái đuôi", track=False)["items"]
    assert any("cue:cái đuôi" in i["reasons"] for i in cued if i["record_id"] == "phase:aml:vault:uoi-nam-ngoai")


def test_title_match_beats_common_words_in_long_bodies(memory):
    filler = " ".join(["nằm ở đâu cũng được"] * 150)
    for n in range(3):
        memory.log_fact(f"long-{n}", title=f"Long archive note {n}", summary="unrelated", content=filler)
    memory.log_phase("tail", title="Đuôi nằm ngoài", summary="The tail lives outside.")
    packet = memory.retrieve("đuôi nằm ở đâu", track=False)
    order = [i["record_id"] for i in packet["items"] if i["domain"] != "core" and i["domain"] != "ontology"]
    assert order[0] == "phase:tail"
    assert "title:" in " ".join(next(i for i in packet["items"] if i["record_id"] == "phase:tail")["reasons"])


def test_pinned_core_leads_the_packet_even_with_long_memories(memory):
    for n in range(4):
        memory.log_fact(f"huge-{n}", title=f"Who are you essay {n}", summary="long", content="who are you " * 800)
    packet = memory.retrieve("who are you", track=False)
    assert [i["record_id"] for i in packet["items"]][:2] == ["core", "vho-open-ontology-core"] or \
        [i["record_id"] for i in packet["items"]][:2] == ["vho-open-ontology-core", "core"]
    assert "self-location" in packet["packet"]


def test_mcp_dispatch(tmp_path):
    server = IdentityServer(IdentityMemory(load_profile("example"), tmp_path / "m.sqlite3"))
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["serverInfo"]["name"] == "trajecta-identity-memory"
    listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert {tool["name"] for tool in listed["result"]["tools"]} == {tool["name"] for tool in TOOLS}
    logged = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "identity_log_phase",
        "arguments": {"event_id": "mcp-probe", "title": "Probe", "summary": "Logged over MCP"},
    }})
    assert logged["result"]["isError"] is False
    got = server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
        "name": "identity_retrieve", "arguments": {"cue": "probe"},
    }})
    assert "phase:mcp-probe" in [i["record_id"] for i in got["result"]["structuredContent"]["items"]]
    bad = server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
        "name": "identity_core_propose", "arguments": {"reason": "x", "phase_context": {}},
    }})
    assert bad["result"]["isError"] is True
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
