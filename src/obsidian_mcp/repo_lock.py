"""Thread and process serialization for persistent repository mutations."""

import fcntl
import threading
from contextlib import contextmanager
from pathlib import Path

_locks: dict[str, threading.RLock] = {}
_guard = threading.Lock()


@contextmanager
def file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with _guard:
        lock = _locks.setdefault(str(path.resolve()), threading.RLock())
    with lock, path.open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
