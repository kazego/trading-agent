"""Local advisory ownership lock, released by the OS on process exit.

Every writer must use the same resolved database path and this lock. This is
not distributed locking; do not put the database on a network filesystem.
"""

import os
from pathlib import Path

from errors import PersistenceUnavailableError, RuntimeOwnershipError


class DatabaseLock:
    def __init__(self, database_path: Path):
        self.path = Path(str(database_path) + ".lock")
        self._file = None

    def acquire(self):
        if self._file is not None:
            raise RuntimeOwnershipError("Database lock already acquired")
        try:
            handle = self.path.open("a+b")
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
        except OSError:
            if "handle" in locals():
                handle.close()
            raise PersistenceUnavailableError("Database lock cannot be opened") from None
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeOwnershipError("Local database is already owned or cannot be locked") from None
        self._file = handle

    def release(self):
        if self._file is not None:
            # Closing releases the OS lock. Never unlink: that creates a race
            # between owners of the old inode and owners of a replacement file.
            self._file.close()
            self._file = None
