"""Python public-writer replay of captured transaction inputs, never SQL logs."""
from pathlib import Path
import shutil
from unittest.mock import patch

import memory_core.store as kernel
import memory_core.governance as governance
import copy
from trajecta_identity.identity import PINNED


def replay_units(fixture: Path, root: Path, units):
    root.mkdir(parents=True, exist_ok=True)
    database = root / "store.sqlite3"
    shutil.copy2(fixture, database)
    store = kernel.MemoryStore(database, pinned_guard=tuple(PINNED))
    for unit in units:
        values = iter(unit["clocks"])
        with patch.object(kernel, "utc_now", lambda: next(values)), patch.object(governance, "utc_now", lambda: next(values)):
            target = governance.ValidatedIntake(store, surface=unit["surface"]) if unit["call"].startswith("_") else store
            getattr(target, unit["call"])(**copy.deepcopy(unit["arguments"]))
        assert next(values, None) is None, "replay did not consume the captured clock sequence"
    return database
