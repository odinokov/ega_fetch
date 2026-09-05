"""Exclusive lock on one output directory.

Two concurrent runs on one output directory would corrupt the ledger and race
each other's staging files, so the directory is held exclusively for a run.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from types import TracebackType
from typing import Optional, TextIO, Type

from ..constants import PROG
from ..errors import LockBusy
from ..util import now_stamp, unlink_quiet


class DirLock:
    """A ``flock``-based exclusive lock, released on context exit."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: Optional[TextIO] = None

    def __enter__(self) -> DirLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("w")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise LockBusy(
                f"another {PROG} run holds the lock on this output directory "
                f"({self.path}); wait for it to finish or remove the file if stale"
            ) from exc
        self._fh = fh
        fh.write(f"pid={os.getpid()} started={now_stamp()}\n")
        fh.flush()
        return self

    def __exit__(self, exc_type: Optional[Type[BaseException]],
                 exc: Optional[BaseException],
                 tb: Optional[TracebackType]) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
            unlink_quiet(self.path)
