"""Host-wide request slots on the shared Docker volume; kernel releases on crash.

No database connection or transaction is involved. All replicas must share the
directory and slot count. Intended for Linux Compose, not multi-host NFS.
"""
import os
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def provider_slot(limit, remaining):
    import fcntl
    directory = Path(os.environ.get("UPLOAD_DIR", "uploads")) / ".provider-slots"
    directory.mkdir(parents=True, exist_ok=True)
    held = None
    try:
        while held is None:
            remaining()
            for index in range(limit):
                handle = (directory / str(index)).open("a")
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    handle.close()
                else:
                    held = handle
                    break
            if held is None:
                time.sleep(min(0.1, remaining()))
        yield
    finally:
        if held is not None:
            held.close()
