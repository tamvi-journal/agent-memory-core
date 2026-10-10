"""Test-only public transaction inputs and deterministic process-kill seams."""
from contextlib import ExitStack, contextmanager
import copy
import inspect
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import memory_core.store as kernel
import memory_core.governance as governance

WRITERS = ("create_current", "revise", "invalidate", "add_relation", "retract_relation",
           "add_cue", "record_access", "apply_maintenance")


def pause():
    sys.__stderr__.write("R3-CRASH-READY\n")
    sys.__stderr__.flush()
    threading.Event().wait()


@contextmanager
def instrument(case, units):
    current = None
    scheduled = False
    committed = 0
    def complete(unit):
        nonlocal committed
        if case.get("capture"):
            units.append(unit)
        committed += 1
        if case.get("crash_after_unit") == committed:
            pause()
    original_now = kernel.utc_now
    def release():
        gate = Path(case["allocation_release"])
        while not gate.exists():
            threading.Event().wait(0.01)
    def now():
        nonlocal scheduled
        if case.get("allocation_role") == "holder" and not scheduled and current is not None and current["call"] in {"revise", "retract_relation"}:
            scheduled = True
            sys.__stderr__.write("R3-ALLOCATION-HOLDER\n")
            sys.__stderr__.flush()
            release()
        if case.get("pause_before_relation_write") and current is not None and current["call"] == "retract_relation":
            sys.__stderr__.write("R3-RELATION-READY\n")
            sys.__stderr__.flush()
            assert sys.__stdin__.buffer.read(1) == b"R"
        value = original_now()
        if current is not None:
            current["clocks"].append(value)
        return value
    with ExitStack() as stack:
        if case.get("allocation_role"):
            original_connect = kernel.MemoryStore._raw_connect
            @contextmanager
            def allocation_connection(self, **kwargs):
                with original_connect(self, **kwargs) as conn:
                    if not kwargs.get("readonly"):
                        def trace(sql):
                            nonlocal scheduled
                            units.append({"call": "sql_trace", "sql": sql})
                            if case["allocation_role"] == "victim" and not scheduled and sql.startswith("BEGIN"):
                                scheduled = True
                                sys.__stderr__.write("R3-ALLOCATION-VICTIM\n")
                                sys.__stderr__.flush()
                                release()
                        conn.set_trace_callback(trace)
                    yield conn
            stack.enter_context(patch.object(kernel.MemoryStore, "_raw_connect", allocation_connection))
        stack.enter_context(patch.object(kernel, "utc_now", now))
        stack.enter_context(patch.object(governance, "utc_now", now))
        for name in ("_capture_evidence", "_insert_intake", "_link_evidence", "_decide_intake"):
            original = getattr(governance.ValidatedIntake, name)
            def intake_unit(self, *args, _name=name, _original=original, **kwargs):
                nonlocal current
                bound = inspect.signature(_original).bind(self, *args, **kwargs)
                payload = copy.deepcopy({k: v for k, v in bound.arguments.items() if k != "self"})
                if _name == "_decide_intake":
                    payload.setdefault("operation_id", None)
                # A null target skips the link helper without opening a transaction.
                if _name == "_link_evidence" and not payload["revision_id"]:
                    return _original(self, *args, **kwargs)
                unit = {"call": _name, "arguments": payload, "surface": self.surface, "clocks": []}
                parent, current = current, unit
                try:
                    result = _original(self, *args, **kwargs)
                finally:
                    current = parent
                complete(unit)
                return result
            stack.enter_context(patch.object(governance.ValidatedIntake, name, intake_unit))
        for name in WRITERS:
            original = getattr(kernel.MemoryStore, name)
            def wrapped(self, *args, _name=name, _original=original, **kwargs):
                nonlocal current
                bound = inspect.signature(_original).bind(self, *args, **kwargs)
                payload = copy.deepcopy({k: v for k, v in bound.arguments.items() if k != "self"})
                unit = {"call": _name, "arguments": payload, "clocks": []}
                parent = current
                current = unit
                try:
                    result = _original(self, *args, **kwargs)
                finally:
                    current = parent
                complete(unit)
                crash = case.get("crash")
                if ((crash == "g10" and _name == "create_current") or
                    (crash == "g12" and _name == "record_access") or
                    (crash == "g9" and _name == "apply_maintenance")):
                    pause()
                return result
            stack.enter_context(patch.object(kernel.MemoryStore, name, wrapped))
        if case.get("crash") == "transaction":
            original = kernel.MemoryStore._raw_connect
            @contextmanager
            def connection(self, **kwargs):
                with original(self, **kwargs) as conn:
                    yield conn
                    if not kwargs.get("readonly") and conn.in_transaction:
                        pause()
            stack.enter_context(patch.object(kernel.MemoryStore, "_raw_connect", connection))
        if case.get("crash") in {"intake-revision", "authority-revision"}:
            original_telemetry = kernel.MemoryStore._insert_telemetry
            def telemetry(*args, **kwargs):
                pause()
                return original_telemetry(*args, **kwargs)
            stack.enter_context(patch.object(kernel.MemoryStore, "_insert_telemetry", staticmethod(telemetry)))
        yield
