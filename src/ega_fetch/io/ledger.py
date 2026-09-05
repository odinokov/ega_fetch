"""Durable record of converged files, so re-runs are cheap."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, Optional

from ..models import Item
from ..util import atomic_write_json, now_stamp, read_json

LedgerEntry = Dict[str, Any]


class Ledger:
    """Durable record of converged files, keyed by decrypted name and holding
    the DECRYPTED size and digest. Never trusted over the filesystem: an entry
    is honoured only if the file is still present at the recorded size."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        raw = read_json(path, default={})
        self._data: Dict[str, LedgerEntry] = raw.get("files", {}) if isinstance(raw, dict) else {}

    def get(self, name: str) -> Optional[LedgerEntry]:
        """Return a copy of the entry for ``name``, or None."""
        with self._lock:
            entry = self._data.get(name)
            return dict(entry) if entry else None

    def record(self, item: Item, size: int, digest: Optional[str]) -> None:
        """Record ``item`` as converged at ``size`` bytes and flush to disk."""
        with self._lock:
            self._data[item.file_name] = {
                "accession": item.accession,
                "sha256": digest or item.sha256,
                "size": size,
                "verified_at": now_stamp(),
            }
            self._flush_locked()

    def forget(self, name: str) -> None:
        """Drop any entry for ``name`` and flush, if one was present."""
        with self._lock:
            if self._data.pop(name, None) is not None:
                self._flush_locked()

    def _flush_locked(self) -> None:
        atomic_write_json(self.path, {"version": 1, "files": self._data})
