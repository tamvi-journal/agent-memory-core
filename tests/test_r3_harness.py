from __future__ import annotations

import os
from pathlib import Path

import pytest

from tools.r3.common import contained, isolated_env
from tools.r3.plans import candidate_orders, fixed_plans, seeded_plan


def test_r3_containment_resolves_symlinked_parent_before_any_write(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "escape").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ValueError, match="outside selected root"):
        contained(root, root / "escape/new/store.sqlite3")
    assert list(outside.iterdir()) == []


def test_r3_child_env_drops_proxies_work_root_and_contains_all_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://invalid.example")
    monkeypatch.setenv("TRAJECTA_WORK_ROOT", "untrusted")
    env = isolated_env(tmp_path)
    assert not any("proxy" in key.lower() for key in env)
    assert "TRAJECTA_WORK_ROOT" not in env
    for key in ("HOME", "USERPROFILE", "XDG_DATA_HOME", "LOCALAPPDATA", "APPDATA",
                "TRAJECTA_IDENTITY_DATA_DIR", "TRAJECTA_IDENTITY_PROFILES", "TMPDIR", "TMP", "TEMP"):
        assert Path(env[key]).resolve().is_relative_to(tmp_path.resolve())
    assert Path(env["TRAJECTA_IDENTITY_PROFILES"], "example/profile.json").is_file()


def test_r3_fixed_matrix_covers_every_surface_pair_and_cross_issued_receipt_kind():
    plans = fixed_plans()
    surfaces = {(r, s) for r in ("py", "ts") for s in ("cli", "mcp")}
    pairs = {tuple(tuple(x) for x in p["pair"]) for p in plans if "pair" in p}
    assert pairs == {(a, b) for a in surfaces for b in surfaces}
    receipts = {(p["receipt"], p["issuer"], p["consumer"]) for p in plans if "receipt" in p}
    assert receipts == {(kind, a, b) for kind in ("apply", "reject", "retract", "legacy-close")
                        for a, b in (("py", "ts"), ("ts", "py"))}


def test_r3_seeded_plans_are_pure_and_use_a_fixed_checked_in_seed_list():
    import json
    seeds = json.loads((Path(__file__).resolve().parents[1] / "tools/r3/seeds.json").read_text())
    assert len(seeds) == len(set(seeds)) == 200
    first = seeded_plan(seeds[0])
    seeded_plan(seeds[1])
    assert seeded_plan(seeds[0]) == first


def test_r3_enumeration_keeps_compound_subtransactions_and_real_time_constraints():
    processes = [["a-submit", "a-cue"], ["b-access", "b-recall"]]
    orders = list(candidate_orders(processes))
    assert len(orders) == 6
    assert ["a-submit", "b-access", "a-cue", "b-recall"] in orders
    constrained = list(candidate_orders(processes, before={("a-cue", "b-access")}))
    assert constrained == [["a-submit", "a-cue", "b-access", "b-recall"]]


@pytest.mark.parametrize("mutation", ["bool-kind", "key-order", "whitespace"])
def test_decay_whitelist_preserves_every_other_details_byte(mutation):
    import copy
    import json
    from tools.r3.common import compare_dumps
    source = Path(__file__).resolve().parents[1] / "spec/golden-writes-v5/decay/dump.json"
    expected = json.loads(source.read_text())
    actual = copy.deepcopy(expected)
    op = next(r for r in actual["tables"]["memory_operations_v3"] if r["idempotency_key"].startswith("maintenance:decay:"))
    details = json.loads(op["details_json"])
    if mutation == "bool-kind":
        details["semantic_content_changed"] = 0
        op["details_json"] = json.dumps(details, ensure_ascii=False)
    elif mutation == "key-order":
        op["details_json"] = json.dumps(dict(reversed(list(details.items()))), ensure_ascii=False)
    else:
        op["details_json"] = op["details_json"].replace(": ", ":", 1)
    with pytest.raises(AssertionError): compare_dumps(expected, actual, decay=True)


def test_decay_whitelist_accepts_only_the_named_numeric_tokens():
    import copy
    import json
    from tools.r3.common import compare_dumps
    source = Path(__file__).resolve().parents[1] / "spec/golden-writes-v5/decay/dump.json"
    expected = json.loads(source.read_text())
    actual = copy.deepcopy(expected)
    op = next(r for r in actual["tables"]["memory_operations_v3"] if r["idempotency_key"].startswith("maintenance:decay:"))
    details = json.loads(op["details_json"])
    details["adjustments"][0]["new_value"] += 1e-7
    op["details_json"] = json.dumps(details, ensure_ascii=False)
    compare_dumps(expected, actual, decay=True)

