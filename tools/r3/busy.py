"""Real process contention without SQL locks, timeout overrides or timed release."""
from contextlib import contextmanager
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def held_writer(runtime, database, env):
    database = Path(database).resolve()
    if runtime == "py":
        command = [sys.executable, "-B", str(ROOT / "tools/r3/holder.py"), str(database)]
    else:
        command = [shutil.which("node"), "--experimental-strip-types",
                   str(ROOT / "tools/r3/holder.ts"), str(database)]
    child = subprocess.Popen(command, cwd=database.parent, env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    ready = queue.Queue()
    reader = threading.Thread(target=lambda: ready.put(child.stdout.readline()), daemon=True)
    reader.start()
    returned = False
    try:
        assert ready.get(timeout=15) == b"ready\n", "holder did not reach revision seam"
        yield child
        # The caller must return from the victim before exiting this context.
        assert child.poll() is None, "holder exited before victim outcome"
        child.stdin.write(b"R")
        child.stdin.flush()
        out, err = child.communicate(timeout=15)
        returned = True
        assert child.returncode == 0 and not out and not err, (child.returncode, out, err)
    finally:
        if not returned:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=15)
        reader.join(timeout=1)
