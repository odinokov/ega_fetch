"""Fast, always-fatal environment checks made before any transfer.

These checks report; they never repair. The private key in particular is the
user's to manage -- a downloader silently relaxing or tightening the mode on a
credential would be a surprising thing to discover after the fact.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import List

from ..errors import PreflightError
from ..models import Remote

REQUIRED_EXECUTABLES = ("sftp", "crypt4gh")


def check_environment(remote: Remote) -> None:
    """Millisecond-scale, always-fatal checks. Runs before any file or network
    I/O so a missing binary is not discovered after an hour of hashing."""
    problems: List[str] = []
    for exe in REQUIRED_EXECUTABLES:
        if shutil.which(exe) is None:
            problems.append(f"{exe!r} not found on PATH"
                            + ("  (pip install crypt4gh)" if exe == "crypt4gh" else ""))

    key = remote.key_path
    if not key.is_file():
        problems.append(f"private key not found: {key}")
    elif key.stat().st_mode & 0o077:
        # Stop here rather than also probing with ssh-keygen: on a key this
        # permissive the probe can only fail for the reason already reported,
        # and it does so as a multi-line ASCII banner that buries the one
        # sentence the user needs.
        problems.append(
            f"private key {key} is group/world accessible; ssh will refuse it. "
            f"Run: chmod 600 {key}"
        )
    else:
        probe = subprocess.run(["ssh-keygen", "-y", "-f", str(key)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if probe.returncode != 0:
            problems.append(f"private key {key} is unusable: {probe.stderr.strip()[:200]}")

    if problems:
        raise PreflightError(*problems)
