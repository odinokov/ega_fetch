"""Small pure helpers: formatting, filesystem probes, hashing, JSON caches."""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional, Protocol, Sequence, Tuple

from .cancel import Canceller
from .constants import IO_CHUNK

log = logging.getLogger(__name__)


class ByteReader(Protocol):
    """The only thing :func:`hash_stream` needs from a source.

    Narrower than :class:`typing.BinaryIO`, which does not declare ``readinto``
    at all -- that omission is why the source of the bytes has to be described
    structurally rather than by a nominal file type.
    """

    def readinto(self, buffer: bytearray, /) -> Optional[int]:
        """Fill ``buffer``; return the byte count, or 0/None at end of stream."""


class ByteSink(Protocol):
    """The only thing :func:`hash_stream` needs from a destination."""

    def write(self, data: memoryview, /) -> object:
        """Write one chunk."""


def human(n: float) -> str:
    """Format a byte count with a binary unit suffix."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"


def hhmmss(seconds: float) -> str:
    """Format a duration, omitting the hours field when it is zero."""
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}h{m:02d}m{s:02d}s" if h else f"{m:d}m{s:02d}s"


def now_stamp() -> str:
    """One definition: this string is the on-disk contract for the ledger,
    the manifest cache and the lock file."""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def preview(names: Sequence[str], n: int = 5) -> str:
    """Join the first ``n`` names, noting how many were elided."""
    head = ", ".join(names[:n])
    return head + (f" (+{len(names) - n} more)" if len(names) > n else "")


def unlink_quiet(path: Path) -> None:
    """Remove if present. A read-only or vanished staging file must never
    mask the real error that is already propagating."""
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def size_or_zero(path: Path) -> int:
    """Size in bytes, 0 if absent. One definition of the 'a missing file is an
    empty file' convention used when planning and when resuming."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def hash_stream(reader: ByteReader, sink: Optional[ByteSink] = None,
                cancel: Optional[Canceller] = None) -> Tuple[str, int]:
    """Consume `reader`, optionally teeing to `sink`; return (sha256, bytes).

    The single definition of this program's read loop: one reusable buffer,
    hashed in place, with one cancellation check per chunk.
    """
    h = hashlib.sha256()
    buf = bytearray(IO_CHUNK)
    view = memoryview(buf)
    total = 0
    while True:
        if cancel is not None:
            cancel.check()
        n = reader.readinto(buf)
        if not n:
            break
        chunk = view[:n]
        if sink is not None:
            sink.write(chunk)
        h.update(chunk)
        total += n
    return h.hexdigest(), total


def sha256_file(path: Path, cancel: Optional[Canceller] = None) -> str:
    """Return the SHA-256 of a file on disk."""
    with path.open("rb") as fh:
        return hash_stream(fh, cancel=cancel)[0]


def read_json(path: Path, *, default: Optional[object] = None) -> object:
    """Read a CACHE file, returning `default` if it is absent, empty,
    unreadable or malformed. Deliberately lossy: caches are rebuildable.
    Not for user-supplied files — see load_config."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return default
    if not text.strip():
        return default
    try:
        return json.loads(text)
    except ValueError as exc:
        log.warning("%s is not valid JSON (%s); ignoring it", path, exc)
        return default


def atomic_write_json(path: Path, payload: object) -> None:
    """Write JSON so a crash never leaves a truncated file in place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        unlink_quiet(Path(tmp))
        raise
