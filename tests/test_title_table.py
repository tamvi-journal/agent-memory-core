"""The frozen str.title() table used by the TS port equals live Python 3.11."""

from __future__ import annotations

import importlib.util
import json
import unicodedata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(unicodedata.unidata_version != "14.0.0", reason="table is frozen at Unicode 14.0.0 (Python 3.11)")
def test_title_table_matches_live_python():
    spec = importlib.util.spec_from_file_location("title_table", ROOT / "tools" / "title_table" / "generate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    frozen = json.loads((ROOT / "node" / "tables" / "py-title-u14.json").read_text(encoding="utf-8"))
    live = module.build()
    for key in ("schema", "unicode", "cased", "title", "lower"):
        assert frozen[key] == live[key], key
