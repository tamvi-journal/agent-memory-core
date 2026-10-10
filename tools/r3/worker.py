"""Test-only public-boundary adapter. Receives only contained synthetic cases."""
from __future__ import annotations

import base64
import contextlib
from datetime import datetime
import io
import json
import os
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.golden import generate as golden
from tools.golden_writes.generate import invoke
from tools.golden_authority.generate import invoke as authority_invoke
from tools.r3.common import contained
from tools.r3.instrument import instrument
from trajecta_identity import IdentityMemory, load_profile
from trajecta_identity.cli import main as cli_main
from trajecta_identity.mcp_server import IdentityServer, serve


class Output(io.StringIO):
    def __init__(self, tty=False):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


class Input:
    def __init__(self, raw, tty):
        self.buffer = io.BytesIO(raw)
        self.tty = tty

    def isatty(self):
        return self.tty

    def readline(self):
        return self.buffer.readline().decode("utf-8", errors="strict")


class Chunks:
    def __init__(self, chunks):
        self.chunks = iter(chunks)

    def read(self, _size):
        return next(self.chunks, b"")


def execute(case):
    root = Path(case["root"]).resolve(strict=True)
    database = Path(case.get("database", "store.sqlite3"))
    contained(root, root / database)
    stdout, stderr = Output(case.get("tty", False)), Output()
    status = 0
    result = None
    units = []
    from unittest.mock import patch
    import shutil
    original_copy = shutil.copy2
    def faulty_copy(source, target, *a, **kw):
        result = original_copy(source, target, *a, **kw)
        if case["backup_fault"] == "readback-missing":
            Path(target).unlink()
            return result
        if case["backup_fault"] == "set":
            raise OSError("synthetic time-set failure")
        os.utime(target, ns=(0, 0))
        return result
    fault = patch.object(shutil, "copy2", faulty_copy) if case.get("backup_fault") else contextlib.nullcontext()
    old_start = golden.START
    old_cwd = Path.cwd()
    os.chdir(root)
    golden.START = datetime.fromisoformat(case.get("clock", "2026-09-30T00:00:00+00:00"))
    try:
        with fault, golden.clock_context(), instrument(case, units), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            if case["surface"] == "cli":
                previous = sys.stdin
                sys.stdin = Input(base64.b64decode(case.get("stdin", "")), case.get("tty", False))
                try:
                    cli_main(case["argv"])
                except SystemExit as exc:
                    if isinstance(exc.code, int): status = exc.code
                    elif exc.code is not None:
                        status = 1
                        print(exc.code, file=sys.stderr)
                finally:
                    sys.stdin = previous
            else:
                memory = IdentityMemory(load_profile("example"), database,
                                        surface="mcp" if case["surface"] == "mcp" else "golden")
                if case["surface"] == "mcp":
                    out = io.BytesIO()
                    serve(IdentityServer(memory), Chunks([base64.b64decode(x) for x in case["chunks"]]), out)
                    stdout.write(out.getvalue().decode("utf-8"))
                elif case["surface"] == "kernel":
                    try:
                        if case["call"] in {"revise", "invalidate", "retract_relation"}:
                            result = getattr(memory.store, case["call"])(**case.get("arguments", {}))
                        elif case["call"] == "identity_close_legacy_discussion":
                            result = memory.identity_close_legacy_discussion(case["arguments"]["receipt_id"])
                        elif case["call"] in {"identity_core_propose", "owner_approve_core", "identity_core_apply", "owner_approve_retract", "identity_retract"}:
                            result = authority_invoke(memory, case["call"], case.get("arguments", {}), {})
                        else:
                            result = invoke(memory, case["call"], case.get("arguments", {}))
                        stdout.write(json.dumps(result, ensure_ascii=False, default=str))
                    except Exception as exc:
                        stdout.write(f"{type(exc).__name__}: {exc}")
                else:
                    raise ValueError("unknown harness surface")
    except Exception:
        status = 1
        traceback.print_exc(file=stderr)
    finally:
        golden.START = old_start
        os.chdir(old_cwd)
    return {"exit": status, "units": units,
            "stdout": base64.b64encode(stdout.getvalue().encode("utf-8", errors="replace")).decode(),
            "stderr": base64.b64encode(stderr.getvalue().encode("utf-8", errors="replace")).decode()}


if __name__ == "__main__":
    # Batch mode is for the fixed Z budgets; each case still has a fresh root.
    for line in sys.stdin.buffer:
        case = json.loads(line)
        old_env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "COMSPEC") if key in os.environ}
        try:
            if "env" in case:
                os.environ.clear()
                os.environ.update(case["env"])
            result = execute(case)
            print(json.dumps(result, separators=(",", ":")), flush=True)
        finally:
            os.environ.clear()
            os.environ.update(old_env)
