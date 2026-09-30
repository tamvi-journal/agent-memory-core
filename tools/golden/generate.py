"""Build the R0b Python oracle corpus in an isolated temporary workspace.

Run with Python 3.11 and Unicode 14. Set PYTHONHASHSEED in the parent process.
The output directory may be checked in or compared byte-for-byte in CI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from memory_core import (  # noqa: E402
    CueDrivenRetriever, MemoryStore, PacketRenderer, SchemaVersionError,
    MigrationRequiredError,
)
from trajecta_identity import IdentityMemory, load_profile  # noqa: E402

START = datetime(2026, 9, 30, 0, 0, 0, tzinfo=timezone.utc)
ORACLE_COMMIT = "def576d"


def canonical(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    # Keep JSONL one physical line even for Python's non-ASCII line separators.
    return (encoded.replace("\u0085", "\\u0085")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def write(path: Path, value: object) -> None:
    path.write_text(canonical(value) + "\n", encoding="utf-8")


def clock_context():
    class Seconds(datetime):
        calls = 0

        @classmethod
        def now(cls, tz=None):
            result = START + timedelta(seconds=cls.calls)
            cls.calls += 1
            return result if tz is None else result.astimezone(tz)

    class Micros(datetime):
        calls = 0

        @classmethod
        def now(cls, tz=None):
            result = START + timedelta(microseconds=cls.calls + 1)
            cls.calls += 1
            return result if tz is None else result.astimezone(tz)

    stack = ExitStack()
    for module in ("memory_core.store", "memory_core.packet", "trajecta_identity.identity"):
        stack.enter_context(patch(f"{module}.datetime", Seconds))
    stack.enter_context(patch("trajecta_identity.activation.datetime", Micros))
    return stack


def sample(mem: IdentityMemory, label: str, cue: str, *, limit=10, budget=2400,
           history=None, history_id=None, scope="global") -> dict:
    store = mem.store
    profile = mem.profile.memory_profile()
    retriever = CueDrivenRetriever(store, profile)
    opts = dict(scope=scope, include_history=history, track_access=False,
                min_accessibility=mem.activation.dormant_below,
                wake_relation_types=("later-phase-of", "caused-by", "depends-on", "decided-because"))
    ranked = retriever.retrieve(cue, limit=1000, token_budget=100000000, **opts)
    selected = retriever.retrieve(cue, limit=limit, token_budget=budget, **opts)

    def hit(item):
        return {"record_id": item.revision["record_id"],
                "score_hex": item.score.hex(), "reasons": item.reasons,
                "history": item.history}

    try:
        packet = PacketRenderer(profile).render(
            cue, selected, scope=scope, surface="golden", token_budget=budget
        )
        packet_error = None
    except ValueError as exc:
        packet, packet_error = None, str(exc)
    try:
        identity = mem.retrieve(cue, limit=limit, token_budget=max(2400, budget),
                                include_history=history, track=False)
    except ValueError as exc:
        identity = {"error": type(exc).__name__, "message": str(exc)}
    status = mem.status()
    status["db"] = "store.sqlite3"
    if status.get("work_store"):
        status["work_store"] = "<temporary-work-store>"
    return {
        "label": label,
        "input": {"cue": cue, "limit": limit, "token_budget": budget,
                  "include_history": history, "scope": scope},
        "current_view": store.current_view(),
        "historical_view": store.historical_view(history_id) if history_id else None,
        "ranked_hits": [hit(item) for item in ranked],
        "selected_hits": [hit(item) for item in selected],
        "packet_text": packet, "packet_error": packet_error,
        "identity_packet_json": identity,
        "status": status, "timeline": mem.timeline(),
        "open_discussions": mem.open_discussions(), "open_loops": mem.open_loops(),
    }


def add_raw(mem: IdentityMemory, record_id: str, *, title="", summary="",
            content="", domain="misc", accessibility=0.6, stability=0.5):
    return mem.store.create_current(
        record_id=record_id, record_class="belief", domain=domain,
        title=title, summary=summary, content=content,
        accessibility=accessibility, stability=stability,
        actor="golden", reason="fixture", evidence={
            "evidence_type": "synthetic", "source_ref": f"fixture:{record_id}",
            "content_summary": "fixture", "confidence": 1.0,
        }, idempotency_key=f"fixture:{record_id}",
    )


def set_access(mem: IdentityMemory, record_id: str, value: float, *, field="accessibility"):
    return mem.store.apply_maintenance(
        run_id=f"fixture:{record_id}:{field}:{value.hex()}",
        adjustments=[{"record_id": record_id, "field": field, "new_value": value}],
        actor="golden", reason="fixture", surface="golden",
    )


def text_scenario(mem):
    mem.bootstrap()
    phrases = [
        ("vietnamese", "Tâm Vi đường ươ à á ả ã ạ", "Ta\u0302m Vi đu\u031bờng u\u031bo\u031b"),
        ("casefold", "ß ẞ ſ İ ς ﬁ", "Casefold versus lower"),
        ("compat", "ＡＢＣ 𝐀 ¼ ² ™ ⑴", "Fullwidth math fraction superscript trademark"),
        ("nonlatin", "漢字 Русский العربية ภาษาไทย", "Non Latin scripts"),
        ("astral", "😀🚀 𝐀" + "😀" * 360, "Emoji and astral slices"),
        ("whitespace", "a\u0085b a\u001cb a\ufeffb a\u200bb a\u3000b", "Whitespace boundaries"),
        ("short", "a b c", "One character words"),
    ]
    for key, title, summary in phrases:
        mem.log_phase(key, title=title, summary=summary, cues=[title])
    queries = [
        ("vi-nfc", "Tâm Vi đường ươ à á ả ã ạ"),
        ("vi-nfd", "Ta\u0302m Vi đu\u031bờng u\u031bo\u031b"),
        ("casefold", "ß ẞ ſ İ ς ﬁ"), ("compat", "ＡＢＣ 𝐀 ¼ ² ™"),
        ("nonlatin", "漢字 Русский العربية ภาษาไทย"),
        ("astral", "😀🚀 𝐀"), ("one-character", "a b c"),
        ("whitespace", "a\u0085b a\u001cb a\ufeffb a\u200bb a\u3000b"),
    ]
    return [sample(mem, label, cue) for label, cue in queries]


def recall_scenario(mem):
    mem.bootstrap()
    for key in ("a-source", "z-source", "middle", "depth-two", "dormant-direct",
                "dormant-causal", "dormant-plain"):
        mem.log_phase(key, title=key, summary="graph fixture", cues=[key])
    for source in ("a-source", "z-source"):
        mem.store.add_cue(profile=mem.profile.name, cue="graph trigger",
                          target_record_id=f"phase:{source}", weight=1.0)
        mem.store.add_relation(relation_id=f"spread:{source}",
            from_record_id=f"phase:{source}", to_record_id="phase:middle",
            relation_type="linked", weight=0.8)
    mem.store.add_relation(relation_id="spread:depth", from_record_id="phase:middle",
        to_record_id="phase:depth-two", relation_type="linked", weight=0.8)
    mem.store.add_relation(relation_id="spread:reverse", from_record_id="phase:z-source",
        to_record_id="phase:depth-two", relation_type="linked", weight=0.07)
    for key, relation in (("dormant-causal", "caused-by"), ("dormant-plain", "linked")):
        mem.store.add_relation(relation_id=f"dormant:{key}",
            from_record_id="phase:a-source", to_record_id=f"phase:{key}",
            relation_type=relation, weight=1.0)
    for key in ("dormant-direct", "dormant-causal", "dormant-plain"):
        set_access(mem, f"phase:{key}", 0.1)
    mem.store.add_cue(profile=mem.profile.name, cue="wake direct",
                      target_record_id="phase:dormant-direct", weight=1.0)
    # Equal-weight rows with a shared recomputed key but distinct stored keys.
    with mem.store.connect() as conn:
        conn.execute("INSERT INTO memory_cues_v3(cue,cue_norm,cue_type,target_record_id,weight,scope,profile) "
                     "VALUES('same cue','old-normalizer','phrase','phase:a-source',1.0,'global',?)",
                     (mem.profile.name,))
        conn.execute("INSERT INTO memory_cues_v3(cue,cue_norm,cue_type,target_record_id,weight,scope,profile) "
                     "VALUES('same cue','same cue','phrase','phase:a-source',1.0,'global',?)",
                     (mem.profile.name,))
    mem.store.add_cue(profile=mem.profile.name, cue="who are you",
                      target_record_id="phase:z-source", weight=2.0)
    mem.store.add_cue(profile=mem.profile.name, cue="aa bb cc dd ee",
                      target_record_id="phase:a-source", weight=1.0)
    mem.log_fact("history", title="History marker", summary="before", cues=["history marker"])
    mem.log_fact("history", title="History marker", summary="after", cues=["history marker"])
    words = "aa bb cc dd ee ff gg hh".split()
    for n in (1, 3, 5, 7):
        add_raw(mem, f"tie:{n}", title=" ".join(words[:n]),
                summary=" ".join(words[:n]))
    for record_id in ("tie:é", "tie:😀"):
        add_raw(mem, record_id, title="equal", summary="equal")
    add_raw(mem, "budget:fat", title="budget", content="word " * 500)
    add_raw(mem, "budget:small", title="budget", content="word")
    queries = [
        ("graph-convergence", "graph trigger", {}),
        ("direct-wake", "wake direct", {}),
        ("equal-cue", "same cue", {}),
        ("alias-versus-stored", "who are you", {}),
        ("exact-cue", "aa bb cc dd ee", {}),
        ("overlap-exactly-0.8", "aa bb cc dd xx", {}),
        ("lexical-ties", "aa bb cc dd ee ff gg hh", {}),
        ("equal-score-codepoint", "equal", {}),
        ("history-marker", "history marker", {"history_id": "fact:history"}),
        ("history-false", "history marker", {"history": False, "history_id": "fact:history"}),
        ("history-true", "history marker", {"history": True, "history_id": "fact:history"}),
        ("budget-skip", "budget", {"budget": 8}),
        ("first-over-budget", "budget", {"budget": 1}),
        ("limit-break", "graph trigger", {"limit": 1}),
    ]
    return [sample(mem, label, cue, **options) for label, cue, options in queries]


def packet_scenario(mem):
    mem.bootstrap()
    add_raw(mem, "unknown:section", title="😀" * 200, summary="漢字" * 100,
            content="é" * 400, domain="unknown_domain")
    mem.store.add_cue(profile=mem.profile.name, cue="unicode section",
                      target_record_id="unknown:section", weight=2.0)
    for n in range(18):
        mem.log_phase(f"many-{n:02d}", title=f"Many memories {n}",
                      summary="memory " * 50, cues=["many memories"])
    profile = mem.profile.memory_profile()
    renderer = PacketRenderer(profile)
    exact = next(n for n in range(64, 1000) if _fits(renderer, "framing", n))
    return [sample(mem, "framing-exact", "framing", budget=exact),
            sample(mem, "budget-64", "framing", budget=64),
            sample(mem, "utf8-heavy", "😀漢字é", budget=500),
            sample(mem, "pinned-past-limit", "many memories", limit=1, budget=2400),
            sample(mem, "unknown-domain", "unicode section", budget=2400)]


def _fits(renderer, cue, budget):
    try:
        renderer.render(cue, [], scope="global", surface="golden", token_budget=budget)
        return True
    except ValueError:
        return False


def identity_scenario(mem, *, closed: bool):
    mem.bootstrap()
    mem.log_phase("first", title="First phase", summary="First light", open_loop=True,
                  cues=["first light"])
    mem.log_phase("second", title="Second phase", summary="A cause followed",
                  follows=["phase:first"], caused_by=["phase:first"],
                  cues=["second light"])
    mem.log_phase("third", title="Third phase", summary="Chain continues",
                  follows=["phase:second"], cues=["third light"])
    outcomes = [mem.log_fact("belief", title="Held fact", summary="before", cues=["held fact"]),
                mem.log_fact("belief", title="Held fact", summary="before"),
                mem.log_fact("belief", title="Held fact", summary="after", cues=["held fact"])]
    revision = mem.revise_core(reason="new phase context",
        phase_context={"model": "oracle", "harness": "golden"})
    recall_outcomes = []
    for label, cue, record_id, start in (
        ("direct", "held fact", "fact:belief", 0.6),
        ("graph-wake", "second light", "phase:first", 0.1),
        ("cap", "third light", "phase:third", 0.9),
    ):
        set_access(mem, record_id, start)
        before = mem.store.current_view(record_id)[0]["accessibility"]
        packet = mem.retrieve(cue, track=True)
        after = mem.store.current_view(record_id)[0]["accessibility"]
        recall_outcomes.append({"label": label, "cue": cue, "record_id": record_id,
                                "before": before.hex(), "after": after.hex(),
                                "reasons": next((item["reasons"] for item in packet["items"]
                                                 if item["record_id"] == record_id), [])})
    if closed:
        mem.close_discussion(note="settled", actor="owner")
        mem.close_loop("phase:first", note="complete", actor="owner")
        mem.retract("phase:third", reason="owner retracted", actor="owner")
    cases = [sample(mem, "phase-chain", "first light", history_id="fact:belief"),
             sample(mem, "fact-refinement", "held fact", history=True, history_id="fact:belief"),
             sample(mem, "core-discussion", "who are you", history_id="core")]
    cases[0]["write_outcomes"] = {"facts": outcomes, "core": revision,
                                  "recall": recall_outcomes,
                                  "closed": closed}
    return cases


def decay_scenario(mem, days: int):
    mem.bootstrap()
    fixtures = []
    moment = START if days == 0 else START + timedelta(days=days, seconds=1000)
    for stability in (0.2, 0.5, 0.9):
        for target in (0.15, 0.35, 0.9):
            for direction in (-1, 0, 1):
                name = f"decay:{stability}:{target}:{direction}"
                add_raw(mem, name, title="decay boundary", accessibility=0.6,
                        stability=stability)
                created = datetime.fromisoformat(mem.store.current_view(name)[0]["created_at"])
                elapsed = max(0.0, (moment - created).total_seconds() / 86400)
                factor = 0.5 ** (elapsed / (21 * (0.5 + stability)))
                raw = (target + direction * 2e-6) / factor
                reachable = 0.0 <= raw <= 1.0
                value = min(1.0, max(0.0, raw))
                set_access(mem, name, value)
                fixtures.append({"record_id": name, "initial": value.hex(),
                                 "target": target, "stability": stability,
                                 "reachable": reachable, "direction": direction})
    for direction in (-1, 0, 1):
        value = 0.9 + 1e-6
        if direction:
            value = math.nextafter(value, math.inf if direction > 0 else -math.inf)
        name = f"epsilon:{direction}"
        add_raw(mem, name, title="epsilon boundary", accessibility=value)
        fixtures.append({"record_id": name, "initial": value.hex(), "target": 0.9})
    result = mem.decay(now=moment.isoformat())
    for item in fixtures:
        if item["record_id"].startswith("epsilon:"):
            original = float.fromhex(item["initial"])
            item["cap_delta_hex"] = (original - 0.9).hex()
    case = sample(mem, f"decay-{days}", "decay boundary", limit=100)
    case["write_outcomes"] = {"decay": result, "fixtures": fixtures}
    return [case]


def encoding_scenario(mem):
    mem.bootstrap()
    context = {"b": 1, "2": 2, "1": 1.0, "float-small": 1e-5,
               "float-large": 1e16, "\ue000": "bmp", "😀": "astral"}
    mem.log_phase("ordered", title="Encoding phase", summary="Order and numbers",
                  phase_context=context, cues=["encoding phase"])
    outcome = mem.revise_core(reason="encoding probe", phase_context=context)
    case = sample(mem, "ordered-json", "encoding phase", history_id="core")
    case["write_outcomes"] = {"core": outcome, "phase_context": context}
    return [case]


def dump_database(path: Path) -> dict:
    if not path.exists() or path.stat().st_size == 0:
        return {"tables": {}}
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        names = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        tables = {}
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            columns = [dict(row) for row in conn.execute(f"PRAGMA table_info({quoted})")]
            pk = [col["name"] for col in sorted(columns, key=lambda col: col["pk"])
                  if col["pk"]]
            order = ",".join('"' + col.replace('"', '""') + '"' for col in pk) if pk else "rowid"
            rows = [dict(row) for row in conn.execute(f"SELECT * FROM {quoted} ORDER BY {order}")]
            tables[name] = [{key: ({"repr": repr(value), "hex": value.hex()}
                                  if isinstance(value, float) else value)
                             for key, value in row.items()} for row in rows]
        return {"tables": tables}
    finally:
        conn.close()


def legacy_v3(path: Path):
    store = MemoryStore(path)
    store.initialize()
    add_raw(type("M", (), {"store": store})(), "legacy", title="Legacy v3", summary="unmigrated")
    add_raw(type("M", (), {"store": store})(), "legacy-target", title="Old target")
    with store._raw_connect() as conn:
        conn.execute("DROP VIEW memory_relation_current_v4")
        conn.execute("DROP TABLE memory_relation_events_v4")
        conn.execute(
            "INSERT INTO memory_relations_v3(relation_id,from_record_id,to_record_id,"
            "relation_type,weight,source_revision_id,status,created_at) "
            "VALUES('old-active','legacy','legacy-target','caused-by',0.7,NULL,'active',"
            "'2026-09-01T00:00:00+00:00')"
        )
        conn.execute("PRAGMA user_version=3")
    return store


def legacy_v2(path: Path):
    with sqlite3.connect(path) as conn:
        conn.executescript("""
        CREATE TABLE memory_records_v2 (
            record_id TEXT PRIMARY KEY, record_class TEXT, domain TEXT, scope TEXT,
            record_status TEXT, created_at TEXT, created_by TEXT);
        CREATE TABLE memory_revisions_v2 (
            revision_id TEXT PRIMARY KEY, record_id TEXT, parent_revision_id TEXT,
            revision_number INTEGER, title TEXT, summary TEXT, content TEXT,
            impact TEXT, confidence REAL, salience REAL, stability REAL,
            accessibility REAL, valid_from TEXT, valid_to TEXT,
            revision_status TEXT, authority_status TEXT, content_sha256 TEXT,
            created_at TEXT, created_by TEXT, surface TEXT, model_family TEXT,
            reason TEXT, idempotency_key TEXT);
        CREATE TABLE memory_evidence_v2 (
            evidence_id TEXT PRIMARY KEY, evidence_type TEXT, source_ref TEXT,
            source_sha256 TEXT, captured_at TEXT, actor TEXT, surface TEXT,
            model_family TEXT, content_summary TEXT, confidence REAL,
            privacy_class TEXT);
        CREATE TABLE memory_revision_evidence_v2 (
            revision_id TEXT, evidence_id TEXT, stance TEXT, weight REAL, reason TEXT);
        """)
        conn.execute("INSERT INTO memory_records_v2 VALUES(?,?,?,?,?,?,?)",
                     ("old-v2", "belief", "semantic", "global", "active",
                      "2026-01-01T00:00:00+00:00", "legacy-agent"))
        conn.execute("INSERT INTO memory_revisions_v2 VALUES(" + ",".join("?" for _ in range(23)) + ")",
                     ("old-v2@r1", "old-v2", None, 1, "Old v2", "Preserved v2", "", "",
                      0.8, 0.7, 0.6, 0.5, "2026-01-01T00:00:00+00:00", None,
                      "current", "canonical_reference", "legacy-semantic-hash",
                      "2026-01-01T00:00:00+00:00", "legacy-agent", "legacy", "",
                      "legacy fixture", "legacy:create"))
        conn.execute("INSERT INTO memory_evidence_v2 VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     ("legacy-evidence", "observation", "legacy:fixture", "legacy-source-hash",
                      "2026-01-01T00:00:00+00:00", "legacy-agent", "legacy", "",
                      "Legacy evidence", 0.8, "synthetic"))
        conn.execute("INSERT INTO memory_revision_evidence_v2 VALUES(?,?,?,?,?)",
                     ("old-v2@r1", "legacy-evidence", "supports", 1.0, "legacy fixture"))


def special_scenario(name: str, path: Path) -> list[dict]:
    store = MemoryStore(path)
    if name in {"legacy-v3", "migrated-v3"}:
        legacy_v3(path)
        if name == "migrated-v3":
            source = path.with_name("source-v3.sqlite3")
            path.rename(source)
            before = source.read_bytes()
            MemoryStore(source).migrate_to(path)
            assert source.read_bytes() == before
            source.unlink()
            result = {"status": "migrated", "current_view": store.current_view(),
                      "active_relations": store.active_relation_rows(),
                      "source_byte_identical": True}
        else:
            before = path.read_bytes()
            try:
                store.current_view()
            except MigrationRequiredError as exc:
                result = {"error": type(exc).__name__, "message": str(exc)}
            else:
                raise AssertionError("legacy v3 read succeeded")
            result["byte_identical_after_read"] = path.read_bytes() == before
            assert result["byte_identical_after_read"]
    elif name == "migrated-v2":
        source = path.with_name("source-v2.sqlite3")
        legacy_v2(source)
        before = source.read_bytes()
        MemoryStore(source).migrate_to(path)
        assert source.read_bytes() == before
        source.unlink()
        result = {"status": "migrated", "current_view": store.current_view(),
                  "source_byte_identical": True}
    elif name == "foreign-app":
        store.initialize()
        with store._raw_connect() as conn:
            conn.execute("PRAGMA application_id=12345")
        try:
            store.current_view()
        except SchemaVersionError as exc:
            result = {"error": type(exc).__name__, "message": str(exc)}
        else:
            raise AssertionError("foreign application_id read succeeded")
    elif name == "future-schema":
        store.initialize()
        with store._raw_connect() as conn:
            conn.execute("PRAGMA user_version=99")
        try:
            store.current_view()
        except SchemaVersionError as exc:
            result = {"error": type(exc).__name__, "message": str(exc)}
        else:
            raise AssertionError("future schema read succeeded")
    elif name == "empty-file":
        path.touch()
        try:
            store.current_view()
        except SchemaVersionError as exc:
            result = {"error": type(exc).__name__, "message": str(exc)}
        else:
            raise AssertionError("empty file read succeeded")
    else:
        raise AssertionError(name)
    return [{"label": name, "input": {"operation": "read"}, "result": result,
             "current_view": result.get("current_view"), "historical_view": None,
             "ranked_hits": None, "selected_hits": None, "packet_text": None,
             "identity_packet_json": None, "status": None, "timeline": None,
             "open_discussions": None, "open_loops": None}]


def generate(output: Path):
    if sys.version_info[:2] != (3, 11) or unicodedata.unidata_version != "14.0.0":
        raise SystemExit("golden generation requires Python 3.11 / Unicode 14.0.0")
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="trajecta-r0b-") as tmp:
        temporary = Path(tmp)
        scenarios = {
            "text": text_scenario, "recall": recall_scenario,
            "packet": packet_scenario,
            "identity-open": lambda mem: identity_scenario(mem, closed=False),
            "identity-closed": lambda mem: identity_scenario(mem, closed=True),
            "encoding": encoding_scenario,
            **{f"decay-{days}": (lambda mem, days=days: decay_scenario(mem, days))
               for days in (0, 7, 21, 400)},
        }
        for name, builder in scenarios.items():
            working = temporary / name
            working.mkdir()
            path = working / "store.sqlite3"
            with clock_context():
                mem = IdentityMemory(load_profile("example"), path, surface="golden")
                cases = builder(mem)
            destination = output / name
            destination.mkdir()
            shutil.copyfile(path, destination / "store.sqlite3")
            write(destination / "dump.json", dump_database(path))
            (destination / "cases.jsonl").write_text(
                "".join(canonical(case) + "\n" for case in cases), encoding="utf-8")
        for name in ("legacy-v3", "migrated-v3", "migrated-v2", "foreign-app", "future-schema", "empty-file"):
            working = temporary / name
            working.mkdir()
            path = working / "store.sqlite3"
            with clock_context():
                cases = special_scenario(name, path)
            destination = output / name
            destination.mkdir()
            shutil.copyfile(path, destination / "store.sqlite3")
            write(destination / "dump.json", dump_database(path))
            (destination / "cases.jsonl").write_text(
                "".join(canonical(case) + "\n" for case in cases), encoding="utf-8")

    corpus = sorted(path for path in output.rglob("*") if path.is_file())
    tables = sorted((ROOT / "memory_core" / "tables").glob("*.json"))
    manifest = {
        "schema": "trajecta.golden-manifest/v1",
        "oracle_commit": ORACLE_COMMIT,
        "python": sys.version.split()[0],
        "unicode": unicodedata.unidata_version,
        "sqlite": sqlite3.sqlite_version,
        # Keys never depend on where --output points or on the OS path
        # separator: corpus files relative to the output root, tables as
        # tables/<name>.
        "files": dict(sorted(
            [(path.relative_to(output).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
             for path in corpus]
            + [(f"tables/{path.name}", hashlib.sha256(path.read_bytes()).hexdigest())
               for path in tables]
        )),
    }
    write(output / "MANIFEST.json", manifest)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "spec" / "golden")
    args = parser.parse_args()
    generate(args.output)
