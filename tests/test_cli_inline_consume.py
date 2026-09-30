"""R2a §4.2 (Q2): when inline consume fails after a durable receipt exists, every
issuing CLI command prints the receipt_id and exits with the exact typed failure,
without retrying or issuing a second receipt."""
from __future__ import annotations

from pathlib import Path

import pytest

from trajecta_identity import StaleAuthority, cli
from trajecta_identity.identity import IdentityMemory

COMMANDS = [
    ("approve-core", ["approve-core", "core-proposal:abc", "--apply"],
     "issue_core_receipt", "identity_core_apply"),
    ("approve-retract", ["approve-retract", "phase:x", "--reason", "r"],
     "issue_retract_receipt", "identity_retract"),
    ("close-legacy-discussion", ["close-legacy-discussion", "--note", "n"],
     "issue_legacy_close_receipt", "identity_close_legacy_discussion"),
]


@pytest.mark.parametrize(("name", "argv", "issuer", "consumer"), COMMANDS, ids=[c[0] for c in COMMANDS])
def test_inline_consume_failure_prints_receipt_and_exact_error(
    tmp_path: Path, monkeypatch, capsys, name, argv, issuer, consumer
):
    calls = {"issue": 0, "consume": 0}

    def issue(self, *args, **kwargs):
        calls["issue"] += 1
        return {"receipt_id": "receipt:" + "d" * 32, "status": "issued"}

    def consume(self, receipt_id):
        calls["consume"] += 1
        assert receipt_id == "receipt:" + "d" * 32
        raise StaleAuthority("target moved after issuance")

    monkeypatch.setattr(IdentityMemory, issuer, issue)
    monkeypatch.setattr(IdentityMemory, consumer, consume)
    with pytest.raises(SystemExit) as exited:
        cli.main(["--profile", "example", "--db", str(tmp_path / "s.sqlite3"), *argv])
    assert str(exited.value) == "StaleAuthority: target moved after issuance"
    assert "receipt:" + "d" * 32 in capsys.readouterr().err
    assert calls == {"issue": 1, "consume": 1}


@pytest.mark.parametrize(("name", "argv", "issuer", "consumer"), COMMANDS, ids=[c[0] for c in COMMANDS])
def test_issue_only_never_consumes(tmp_path: Path, monkeypatch, capsys, name, argv, issuer, consumer):
    monkeypatch.setattr(IdentityMemory, issuer, lambda self, *a, **k: {"receipt_id": "receipt:" + "e" * 32})
    monkeypatch.setattr(IdentityMemory, consumer, lambda self, r: pytest.fail("consumed with --issue-only"))
    cli.main(["--profile", "example", "--db", str(tmp_path / "s.sqlite3"), *argv, "--issue-only"])
    assert "receipt:" + "e" * 32 in capsys.readouterr().out
