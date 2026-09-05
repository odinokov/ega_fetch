"""A stand-in for the ``sftp`` executable, covering only what ega_fetch uses.

The real client is driven as ``sftp <options> -b - <host>`` with a command
script on stdin, so substituting it needs no change to the code under test --
which is the point: the transport is exercised through the same argv and the
same stdin protocol as in production.

Supported commands: ``cd DIR``, ``ls -1``, ``reget REMOTE LOCAL``, ``quit``.
Remote paths resolve against ``$FAKE_SFTP_ROOT``. Neither remote paths nor the
local destination may contain a space; the fixtures never generate one.

Environment:
    FAKE_SFTP_ROOT        directory standing in for the account's outbox
    FAKE_SFTP_STATE       directory for per-destination transfer markers
    FAKE_SFTP_FIRST_CHUNK if set, the first ``reget`` of any destination
                          transfers only this many bytes, so the resume path
                          can be exercised deterministically
    FAKE_SFTP_FAIL_LOGIN  if set, exit non-zero before reading commands
"""

import hashlib
import os
import pathlib
import sys


def _fail(message: str) -> "int":
    sys.stderr.write(message + "\n")
    return 1


def _reget(root: pathlib.Path, state: pathlib.Path, remote: str, local: str) -> int:
    src = root / remote
    if not src.is_file():
        return _fail(f"File \"/{remote}\" not found.")
    dest = pathlib.Path(local)
    have = dest.stat().st_size if dest.exists() else 0

    first_chunk = os.environ.get("FAKE_SFTP_FIRST_CHUNK")
    limit = src.stat().st_size
    if first_chunk:
        marker = state / hashlib.sha256(local.encode()).hexdigest()
        if not marker.exists():
            marker.write_text("seen")
            limit = min(int(first_chunk), limit)

    if have >= limit:
        return 0
    with src.open("rb") as fin, dest.open("ab") as fout:
        fin.seek(have)
        fout.write(fin.read(limit - have))
    return 0


def main() -> int:
    if os.environ.get("FAKE_SFTP_FAIL_LOGIN"):
        return _fail("Permission denied (publickey).")

    root = pathlib.Path(os.environ["FAKE_SFTP_ROOT"])
    state = pathlib.Path(os.environ.get("FAKE_SFTP_STATE", "/tmp"))
    state.mkdir(parents=True, exist_ok=True)
    cwd = root

    for raw in sys.stdin.read().splitlines():
        line = raw.strip()
        if not line or line == "quit":
            continue
        if line.startswith("cd "):
            cwd = root / line[3:].strip()
            if not cwd.is_dir():
                return _fail("Couldn't stat remote file: No such file or directory")
        elif line == "ls -1":
            for entry in sorted(cwd.iterdir()):
                sys.stdout.write(entry.name + "\n")
        elif line.startswith("reget "):
            remote, _, local = line[len("reget "):].strip().partition(" ")
            if not local:
                return _fail(f"invalid reget: {line}")
            rc = _reget(root, state, remote, local)
            if rc:
                return rc
        else:
            return _fail(f"Invalid command: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
