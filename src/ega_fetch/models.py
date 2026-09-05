"""Frozen and mutable records describing one run and one file."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import List, Optional

from .constants import ENC_SUFFIX


@dataclasses.dataclass(frozen=True)
class FileMeta:
    """What the archive publishes about one file."""
    sha256: str      # of the DECRYPTED bytes
    enc_size: int    # of the .c4gh object, i.e. what SFTP will transfer


@dataclasses.dataclass(frozen=True)
class Remote:
    """Where and as whom to connect to the Live Outbox."""
    host: str
    port: int
    username: str
    key_path: Path

    def ssh_opts(self) -> List[str]:
        """Return the ``-o`` option list shared by every ``sftp`` invocation."""
        # -o User= avoids parsing ambiguity: EGA usernames contain '@'.
        return [
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=30",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=4",
            "-o", "IdentitiesOnly=yes",
            "-o", f"User={self.username}",
            "-o", f"Port={self.port}",
            "-i", str(self.key_path),
        ]


@dataclasses.dataclass(frozen=True)
class Settings:
    """Every knob this run uses, resolved once from CLI over config file.

    One mechanism for all settings: there is no reason `jobs` should be
    config-addressable and `io_timeout` not.
    """
    csv_path: Path
    out_dir: Path
    remote: Remote
    dataset: str
    c4gh_key: Path
    jobs: int
    io_timeout: int
    verify: bool
    recheck: bool
    refresh_manifest: bool
    keep_encrypted: bool
    dry_run: bool


@dataclasses.dataclass
class Item:
    """One file's desired end state and what is known about it."""
    file_name: str                    # decrypted name, e.g. CGPLOV621P.hg19.cram
    accession: str                    # EGAF...
    sha256: Optional[str] = None      # expected, from the metadata API
    enc_size: Optional[int] = None    # .c4gh size, from the metadata API

    @property
    def enc_name(self) -> str:
        """The name of the encrypted object as staged on the outbox."""
        return self.file_name + ENC_SUFFIX


@dataclasses.dataclass
class Decision:
    """The planner's verdict for one item. `reason` is reported, so an operator
    can see whether the ledger is working or everything is being rehashed."""
    item: Item
    converged: bool
    reason: str = ""


@dataclasses.dataclass
class Outcome:
    """What happened to one item during :func:`~ega_fetch.core.execute.execute`."""
    item: Item
    ok: bool
    detail: str = ""
    bytes_moved: int = 0
    seconds: float = 0.0
