"""The single-file state machine: transfer, decrypt, verify, publish."""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import BinaryIO, Tuple, cast

from ..cancel import Canceller
from ..constants import STAGING_DIRNAME
from ..errors import FileFailure
from ..io.ledger import Ledger
from ..io.sftp import Sftp
from ..models import Decision, Item, Outcome, Settings
from ..util import ByteReader, hash_stream, human, sha256_file, size_or_zero, unlink_quiet

log = logging.getLogger(__name__)


class Converger:
    """Owns the per-file decision and the per-file convergence sequence."""

    def __init__(self, sftp: Sftp, settings: Settings, ledger: Ledger,
                 cancel: Canceller) -> None:
        self.sftp = sftp
        self.s = settings
        self.ledger = ledger
        self.cancel = cancel
        self.staging = settings.out_dir / STAGING_DIRNAME

    # -- planning ----------------------------------------------------------

    def classify(self, item: Item) -> Decision:
        """Decide once whether the target state already holds. May hash a
        multi-GB file, so the worker consumes the verdict rather than
        re-deriving it."""
        target = self.s.out_dir / item.file_name
        size = size_or_zero(target)
        if size == 0:
            return Decision(item, False)

        entry = self.ledger.get(item.file_name)
        if entry and not self.s.recheck:
            if entry.get("size") == size and (not item.sha256
                                              or entry.get("sha256") == item.sha256):
                return Decision(item, True, "ledger")
            self.ledger.forget(item.file_name)
        if not self.s.verify or not item.sha256:
            return Decision(item, True, "present")

        if sha256_file(target, self.cancel) == item.sha256:
            self.ledger.record(item, size, item.sha256)
            return Decision(item, True, "rehashed")
        log.warning("%s: on-disk checksum differs from the manifest; will re-fetch",
                    item.file_name)
        self.ledger.forget(item.file_name)
        return Decision(item, False)

    # -- the single-file state machine -------------------------------------

    def converge(self, item: Item) -> Outcome:
        """Move one file to the target state, resuming from wherever it stopped."""
        started = time.monotonic()
        enc = self.staging / item.enc_name
        part = self.staging / (item.file_name + ".part")
        before = size_or_zero(enc)

        # 1. transfer (resumes if a partial file is present)
        if item.enc_size is None or before < item.enc_size:
            if before:
                log.info("%s: resuming at %s", item.file_name, human(before))
            self.sftp.reget(self.s.dataset, item.enc_name, enc, self.s.io_timeout)
        got = self._transferred_size(enc, item)

        # 2. decrypt into staging, hashing the stream as it is written:
        #    the bytes are in hand exactly once, so never read them back.
        digest, size = self.decrypt(enc, part)

        # 3. verify before publishing
        if self.s.verify and item.sha256 and digest != item.sha256:
            unlink_quiet(part)
            raise FileFailure(f"SHA-256 mismatch: expected {item.sha256}, got {digest}")

        # 4. publish atomically, then clean up
        os.replace(part, self.s.out_dir / item.file_name)
        if not self.s.keep_encrypted:
            unlink_quiet(enc)

        self.ledger.record(item, size, digest)
        return Outcome(item, True, "", got - before, time.monotonic() - started)

    @staticmethod
    def _transferred_size(enc: Path, item: Item) -> int:
        """Assert the encrypted object arrived whole, and return its size."""
        got = size_or_zero(enc)
        if got == 0:
            raise FileFailure("transfer produced no file")
        if item.enc_size is not None and got != item.enc_size:
            raise FileFailure(
                f"size mismatch after transfer: got {got}, "
                f"the archive publishes {item.enc_size}"
            )
        return got

    def decrypt(self, src: Path, dst: Path) -> Tuple[str, int]:
        """Stream crypt4gh output to `dst`, returning (sha256, bytes written)."""
        cmd = ["crypt4gh", "decrypt", "--sk", str(self.s.c4gh_key)]
        # stderr to a file, not a pipe: a pipe could fill and deadlock while
        # we are draining stdout.
        with src.open("rb") as fin, dst.open("wb") as fout, \
                tempfile.TemporaryFile("w+") as errf:
            proc = subprocess.Popen(cmd, stdin=fin, stdout=subprocess.PIPE, stderr=errf)
            with self.cancel.child(proc):
                # stdout=PIPE guarantees a pipe, and at runtime it is a
                # BufferedReader; typeshed models it as optional and does not
                # declare readinto on IO[bytes]. Both seams are narrowed once,
                # here, so nothing below re-tests an invariant already held.
                pipe = cast(BinaryIO, proc.stdout)
                try:
                    digest, written = hash_stream(cast(ByteReader, pipe), fout, self.cancel)
                except BaseException:
                    proc.kill()
                    proc.wait()
                    unlink_quiet(dst)
                    raise
                finally:
                    pipe.close()
                proc.wait()
            if proc.returncode != 0:
                self.cancel.check()
                errf.seek(0)
                unlink_quiet(dst)
                raise FileFailure(
                    f"crypt4gh exit {proc.returncode}: {errf.read().strip()[:300]}"
                )
        if written == 0:
            unlink_quiet(dst)
            raise FileFailure("decryption produced an empty file")
        return digest, written
