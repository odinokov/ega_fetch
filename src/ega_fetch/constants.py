"""Constants: program identity, network defaults, on-disk names, I/O sizing.

Every value here is part of a contract with something outside the process --
the user (``PROG`` appears in ``--help`` and in log lines), the filesystem (the
``*_NAME`` files), or the archive (``METADATA_BASE``). Changing one is a
breaking change, not a refactor.
"""

from __future__ import annotations

import errno
from typing import FrozenSet

#: Program name. Appears in ``--help``, in every log line's logger tree, and in
#: the on-disk lock file name. Not the same thing as the distribution name
#: (``ega-fetch``) or the console script (``ega-fetch``).
PROG = "ega_fetch"
VERSION = "2.0.0"

DEFAULT_HOST = "outbox.ega-archive.org"
DEFAULT_PORT = 22
DEFAULT_IO_TIMEOUT = 14400
METADATA_BASE = "https://metadata.ega-archive.org"

ENC_SUFFIX = ".c4gh"
STAGING_DIRNAME = ".staging"
LEDGER_NAME = ".ega_state.json"
MANIFEST_NAME = ".ega_manifest.json"
MANIFEST_VERSION = 2          # bumped when the cached schema changes
LOCK_NAME = ".ega_fetch.lock"

IO_CHUNK = 4 * 1024 * 1024

# Errors that no retry will fix and that will recur on every remaining file,
# so the first one aborts the run rather than failing 400 more times.
FATAL_ERRNOS: FrozenSet[int] = frozenset({errno.ENOSPC, errno.EDQUOT, errno.EROFS})
