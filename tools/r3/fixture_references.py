"""Retain exact transient CLI oracle files for PR30 derived-hash byte proofs."""
from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch


def generate(output: Path, references: Path) -> None:
    from tools.golden_cli import generate as cli

    original = cli.database_classes

    def capture(work, initial, argv):
        result = original(work, initial, argv)
        for relative, row in result.items():
            if "oracle_sha256" not in row:
                continue
            raw = (work / relative).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == row["oracle_sha256"]
            target = references / work.name / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        return result

    # Read-only capture at an existing final inspection seam, before temp cleanup.
    # No SQL, clock calls, wire edits or changes to the ordinary corpus inventory.
    with patch.object(cli, "database_classes", capture):
        cli.generate(output)
