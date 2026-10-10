from tools.r3.fuzz import generate, shrink
import pytest


def test_cli_cap_is_enforced_by_generator_not_only_tests(monkeypatch):
    from tools.r3 import fuzz
    monkeypatch.setitem(fuzz.CAPS, "cli_bytes", 1)
    with pytest.raises(AssertionError): generate("cli", 451714940, 0)


def test_cli_scenario_shrinking_keeps_changed_argv():
    from tools.r3.fuzz import render_scenario_argv
    import json
    from tools.r3.fuzz import ROOT
    scenario = ROOT / "spec/golden-cli-v1/authority-core-apply-issue-only"
    registry = json.loads((scenario / "tokens.json").read_text())
    argv = json.loads((scenario / "argv.json").read_text())
    shortened = argv[:-1]
    result = render_scenario_argv(shortened, registry, "/synthetic-root")
    assert len(result) == len(shortened)


def test_generators_are_pure_bounded_and_preserve_explicit_chunks():
    import base64
    import json
    for boundary in ("mcp", "cli", "kernel"):
        for index in range(40):
            first = generate(boundary, 451714940, index)
            generate(boundary, 42, index)
            assert generate(boundary, 451714940, index) == first
            if boundary == "mcp":
                chunks = [base64.b64decode(c) for c in first["chunks"]]
                raw = b"".join(chunks)
                assert len(raw) <= 65536 and len(chunks) <= 16 and raw.count(b"\n") <= 16
            elif boundary == "cli":
                assert sum(len(x.encode()) for x in first["argv"]) + len(base64.b64decode(first.get("stdin", ""))) <= 8192
            else:
                p = first["arguments"]["proposal"]
                assert len(json.dumps(p).encode()) <= 65536 and len(p["evidence"]) <= 32


def test_shrinker_replays_predicate_and_is_deterministic():
    case = {"surface": "mcp", "chunks": ["YWJjWAo="]}
    import base64
    fails = lambda c: b"X" in b"".join(base64.b64decode(x) for x in c["chunks"])
    assert shrink(case, fails) == {"surface": "mcp", "chunks": ["WA=="]}
