"""EGA Live Outbox transport, driving the ``sftp`` executable."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import FrozenSet, Sequence, Set

from ..cancel import Canceller
from ..errors import FileFailure, PreflightError
from ..models import Remote


class Sftp:
    """One sftp process per invocation. Holds the login banner it learned at
    connect() so error text can be reported without pattern-matching prose."""

    def __init__(self, remote: Remote, cancel: Canceller) -> None:
        self.remote = remote
        self.cancel = cancel
        self._banner: FrozenSet[str] = frozenset()

    def run(self, commands: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
        """Run a batch of sftp commands, registered so a stop can terminate it."""
        argv = ["sftp", *self.remote.ssh_opts(), "-b", "-", self.remote.host]
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        with self.cancel.child(proc):
            try:
                out, err = proc.communicate("\n".join([*commands, "quit"]) + "\n",
                                            timeout=timeout)
            except BaseException:
                proc.kill()
                proc.communicate()
                raise
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)

    def connect(self, timeout: int = 90) -> None:
        """Prove the credentials work and record the login banner verbatim."""
        proc = self.run([], timeout)
        self._banner = frozenset(ln.strip() for ln in proc.stderr.splitlines() if ln.strip())
        if proc.returncode != 0:
            self.cancel.check()
            raise PreflightError(
                f"cannot log in to {self.remote.host} as {self.remote.username} "
                f"(sftp exit {proc.returncode}):\n{self.diagnostics(proc)}"
            )

    def diagnostics(self, proc: subprocess.CompletedProcess[str], lines: int = 8) -> str:
        """Error context with the known banner subtracted by exact match."""
        out = [ln for ln in proc.stderr.splitlines()
               if ln.strip() and ln.strip() not in self._banner]
        out += [ln for ln in proc.stdout.splitlines() if ln.strip()]
        return "\n".join(f"    {ln}" for ln in out[-lines:]) or "    (no output)"

    def names(self, dataset: str, timeout: int = 180) -> Set[str]:
        """The set of file names staged for this account.

        Presence is the one question SFTP can answer that the metadata API
        cannot; sizes and checksums come from the API, so `ls -1` suffices and
        no column parsing is needed (a file name may legally contain spaces).
        """
        proc = self.run([f"cd {dataset}", "ls -1"], timeout)
        if proc.returncode != 0:
            self.cancel.check()
            raise PreflightError(
                f"could not list {dataset} on {self.remote.host} "
                f"(sftp exit {proc.returncode}):\n{self.diagnostics(proc)}"
            )
        names = {ln.strip() for ln in proc.stdout.splitlines()
                 if ln.strip() and not ln.startswith("sftp>")}
        if not names:
            raise PreflightError(f"{dataset} contains no files on {self.remote.host}")
        return names

    def reget(self, dataset: str, enc_name: str, dest: Path, timeout: int) -> None:
        """Resumable download: `reget` continues a partial local file."""
        proc = self.run([f"reget {dataset}/{enc_name} {dest}"], timeout)
        if proc.returncode != 0:
            # If we terminated it ourselves, say so rather than blaming sftp.
            self.cancel.check()
            raise FileFailure(f"sftp exit {proc.returncode}:\n{self.diagnostics(proc, 6)}")
