"""Synthetic holder at the existing revision-insert seam; stdin releases it."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from memory_core import MemoryStore
from tools.golden.generate import clock_context


def main():
    store = MemoryStore(sys.argv[1])
    original = MemoryStore._insert_revision

    def paused(connection, revision):
        original(connection, revision)
        sys.stdout.buffer.write(b"ready\n")
        sys.stdout.buffer.flush()
        if sys.stdin.buffer.read(1) != b"R":
            raise RuntimeError("holder released without the parent handshake")

    store._insert_revision = paused
    with clock_context():
        store.create_current(
            record_id="p16-holder", record_class="belief", domain="fact",
            title="Synthetic holder", actor="synthetic", reason="P16 contention",
            evidence={"source_ref": "synthetic:p16"}, idempotency_key="p16-holder",
        )


if __name__ == "__main__":
    main()
