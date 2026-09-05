"""ega_fetch -- converge a local directory to hold a verified copy of an EGA dataset.

Target state, per file listed in the input CSV::

    <out>/<file_name>   exists, is non-empty, and its SHA-256 equals the
                        unencrypted checksum published by the EGA metadata API

The program moves the output directory from an unknown initial state to that
target state and does nothing else. Every step is resumable and re-runnable:
interrupting it at any point and running the same command again converges from
wherever it stopped.

Transport is EGA Live Outbox (SFTP over ssh) plus Crypt4GH decryption. Requires
the ``sftp`` and ``crypt4gh`` executables on PATH. Standard library only
otherwise.

Exit codes
    0   converged: every requested file is present and verified
    1   partial: at least one file failed (others may have succeeded)
    2   usage or configuration error
    3   preflight failed; nothing was transferred
    4   another run holds this output directory; retry later
  130   interrupted

The Python API and the ``ega-fetch`` console script share one implementation::

    from ega_fetch import Canceller, Remote, Settings, run
    exit_code = run(settings, Canceller())
"""

from __future__ import annotations

from .cancel import Canceller
from .constants import VERSION as __version__
from .core.run import run
from .errors import Cancelled, ConfigError, FileFailure, LockBusy, PreflightError
from .models import FileMeta, Item, Outcome, Remote, Settings

__all__ = [
    "Canceller",
    "Cancelled",
    "ConfigError",
    "FileFailure",
    "FileMeta",
    "Item",
    "LockBusy",
    "Outcome",
    "PreflightError",
    "Remote",
    "Settings",
    "__version__",
    "run",
]
