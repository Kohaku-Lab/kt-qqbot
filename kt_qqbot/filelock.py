"""Advisory file locks (``fcntl`` on POSIX, ``msvcrt`` on Windows)."""

import os
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class FileLock:
    """An exclusive lock on a file, held until :meth:`release`."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        """Return whether this object holds the lock."""
        return self._fd is not None

    def acquire(self, *, blocking: bool = True) -> bool:
        """Take the lock; with ``blocking=False`` return False when it is taken."""
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                msvcrt.locking(fd, mode, 1)
            else:
                flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
                fcntl.flock(fd, flags)
        except OSError:
            os.close(fd)
            if blocking:
                raise
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        """Drop the lock."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if os.name == "nt":
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
