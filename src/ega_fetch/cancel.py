"""Run-wide cooperative cancellation that can reach into child processes."""

from __future__ import annotations

import contextlib
import subprocess
import threading
from typing import Any, Iterator, Set

from .errors import Cancelled


class Canceller:
    """Run-wide cooperative stop that can reach into running subprocesses.

    Checking a flag between chunks is not enough on its own: a single `sftp`
    or `crypt4gh` child can block for hours, so every child is registered here
    and stop() terminates them. Terminating mid-transfer is safe by
    construction — `reget` resumes the partial file on the next run.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._children: Set[subprocess.Popen[Any]] = set()

    @property
    def stopped(self) -> bool:
        """True once :meth:`stop` has been called."""
        return self._event.is_set()

    def check(self) -> None:
        """Raise :class:`~ega_fetch.errors.Cancelled` if the run is stopping."""
        if self._event.is_set():
            raise Cancelled

    def stop(self) -> None:
        """Set the stop flag and terminate every registered child process."""
        self._event.set()
        with self._lock:
            children = list(self._children)
        for proc in children:
            with contextlib.suppress(OSError):
                proc.terminate()

    @contextlib.contextmanager
    def child(self, proc: subprocess.Popen[Any]) -> Iterator[subprocess.Popen[Any]]:
        """Register a child for the duration of its run."""
        with self._lock:
            self._children.add(proc)
        try:
            if self._event.is_set():
                # stop() ran between our decision to spawn and registration.
                with contextlib.suppress(OSError):
                    proc.terminate()
            yield proc
        finally:
            with self._lock:
                self._children.discard(proc)
