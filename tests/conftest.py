"""Shared fixtures: a synthetic EGA dataset that never touches the network.

Three seams are substituted, and nothing else:

* ``sftp``      -> :mod:`fake_sftp`, placed first on ``PATH``.
* the metadata API -> a localhost stub; ``METADATA_BASE`` is rebound in the
  child process before ``main()`` runs.
* ``crypt4gh``  -> **not** substituted. The real binary encrypts the fixture and
  decrypts it again, so the cryptographic path is exercised for real, with an
  ssh ed25519 key doubling as the Crypt4GH secret key exactly as EGA does it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

import pytest

DATASET = "EGAD50000000000"
HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parent

#: Deterministic content, and sized to straddle the 4 MiB read chunk so the
#: streaming hash is exercised across several iterations plus a partial one.
FILE_SIZES = (1024, 100_000, 4 * 1024 * 1024 + 7)

#: Values that legitimately differ between two runs of the same code: the log
#: timestamp, elapsed times, throughput, and the filesystem's free space. None
#: of them is part of the specification, so they are normalised rather than
#: compared. Everything else in the log text is compared verbatim.
_SIZE = r"[\d.]+ [KMGTP]?B"
_DURATION = r"\d+(?:h\d{2})?m\d{2}s"
NOISE = [
    (re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ", re.M), ""),
    (re.compile(rf" in {_DURATION}, {_SIZE}/s\)"), " in <TIME>, <RATE>)"),
    (re.compile(rf" in {_DURATION} \({_SIZE}/s average\)"), " in <TIME> (<RATE> average)"),
    (re.compile(rf"{_SIZE} free in"), "<FREE> free in"),
]


def scrub_text(text: str) -> str:
    """Remove wall-clock and filesystem noise from captured output."""
    for pattern, replacement in NOISE:
        text = pattern.sub(replacement, text)
    return text


@dataclass(frozen=True)
class FixtureFile:
    """One synthetic file, in both its plaintext and encrypted forms."""
    name: str
    accession: str
    plain: bytes
    sha256: str
    enc_size: int


@dataclass
class Dataset:
    """Everything a run needs: a key, an outbox, a CSV, and a manifest."""
    key: Path
    outbox: Path
    csv: Path
    files: List[FixtureFile]
    manifest_url_base: str
    bindir: Path
    state: Path

    def records(self) -> List[Dict[str, Any]]:
        """The metadata payload the stub server publishes."""
        return [
            {
                "accession_id": f.accession,
                "unencrypted_checksum": f.sha256,
                "unencrypted_checksum_type": "SHA256",
                "filesize": f.enc_size,
            }
            for f in self.files
        ]


class _Handler(BaseHTTPRequestHandler):
    records: Sequence[Dict[str, Any]] = ()

    def do_GET(self) -> None:  # noqa: N802  (http.server's required spelling)
        if f"/datasets/{DATASET}/files" not in self.path:
            self.send_error(404)
            return
        body = json.dumps(list(self.records)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        """Silence the default stderr access log."""


@pytest.fixture(scope="session")
def crypt4gh() -> str:
    """Path to the real crypt4gh binary, or skip the whole session."""
    found = shutil.which("crypt4gh")
    if found is None:
        pytest.skip("crypt4gh is not on PATH")
    return found


def _make_key(dirpath: Path) -> Path:
    key = dirpath / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key), "-q", "-C", "fixture"],
        check=True,
    )
    key.chmod(0o600)
    return key


def _encrypt(crypt4gh_bin: str, key: Path, plain: bytes, dest: Path) -> int:
    proc = subprocess.run(
        [crypt4gh_bin, "encrypt", "--sk", str(key), "--recipient_pk", str(key) + ".pub"],
        input=plain, capture_output=True, check=True,
    )
    dest.write_bytes(proc.stdout)
    return len(proc.stdout)


@pytest.fixture
def dataset(tmp_path: Path, crypt4gh: str) -> Iterator[Dataset]:
    """Build a complete synthetic dataset and serve its manifest on localhost."""
    key = _make_key(tmp_path)
    outbox = tmp_path / "outbox" / DATASET
    outbox.mkdir(parents=True)

    files: List[FixtureFile] = []
    for n, size in enumerate(FILE_SIZES):
        # Deterministic, incompressible-ish, and distinct per file.
        plain = hashlib.sha256(f"seed-{n}".encode()).digest() * (size // 32 + 1)
        plain = plain[:size]
        name = f"sample{n}.hg19.cram"
        enc_size = _encrypt(crypt4gh, key, plain, outbox / (name + ".c4gh"))
        files.append(FixtureFile(
            name=name,
            accession=f"EGAF5000000000{n}",
            plain=plain,
            sha256=hashlib.sha256(plain).hexdigest(),
            enc_size=enc_size,
        ))

    csv = tmp_path / "sample_file.csv"
    csv.write_text(
        "sample_accession_id,sample_alias,file_name,file_accession_id\n"
        + "".join(f"EGAN{i},alias{i},{f.name},{f.accession}\n" for i, f in enumerate(files))
    )

    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "sftp"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{HERE / "fake_sftp.py"}" "$@"\n')
    shim.chmod(0o755)

    ds = Dataset(key=key, outbox=outbox, csv=csv, files=files,
                 manifest_url_base="", bindir=bindir, state=tmp_path / "shimstate")

    _Handler.records = ds.records()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    ds.manifest_url_base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield ds
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# running the two implementations
# ---------------------------------------------------------------------------

_BOOTSTRAP = """
import sys
import ega_fetch.io.manifest as manifest
manifest.METADATA_BASE = {base!r}
from ega_fetch.cli import main
sys.exit(main())
"""


@dataclass(frozen=True)
class Run:
    """One completed invocation, normalised for comparison."""
    code: int
    stdout: str
    stderr: str
    files: Dict[str, str]      # published file name -> sha256
    ledger: Optional[Dict[str, Any]]
    manifest: Optional[Dict[str, Any]]
    staging: List[str]


def _stable_json(path: Path, volatile: Sequence[str]) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    data: Dict[str, Any] = json.loads(path.read_text())

    def scrub(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: ("<T>" if k in volatile else scrub(v)) for k, v in node.items()}
        if isinstance(node, list):
            return [scrub(v) for v in node]
        return node

    return dict(scrub(data))


def snapshot(out: Path, code: int, stdout: str, stderr: str) -> Run:
    """Reduce an output directory and a process result to comparable values."""
    published = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(out.iterdir())
        if p.is_file() and not p.name.startswith(".")
    }
    staging = sorted(p.name for p in (out / ".staging").iterdir()) \
        if (out / ".staging").is_dir() else []
    return Run(
        code=code,
        stdout=scrub_text(stdout).replace(str(out), "<OUT>"),
        stderr=scrub_text(stderr).replace(str(out), "<OUT>"),
        files=published,
        ledger=_stable_json(out / ".ega_state.json", ("verified_at",)),
        manifest=_stable_json(out / ".ega_manifest.json", ("fetched_at",)),
        staging=staging,
    )


def invoke(ds: Dataset, out: Path, *args: str,
           env: Optional[Dict[str, str]] = None) -> Run:
    """Run the CLI against the fixture and return a normalised snapshot."""
    code = _BOOTSTRAP.format(base=ds.manifest_url_base)
    argv = [
        "--csv", str(ds.csv), "--out", str(out),
        "--username", "fixture@example.org", "--key", str(ds.key),
        "--dataset", DATASET, *args,
    ]
    child_env = dict(
        os.environ,
        PATH=f"{ds.bindir}{os.pathsep}{os.environ['PATH']}",
        FAKE_SFTP_ROOT=str(ds.outbox.parent),
        FAKE_SFTP_STATE=str(ds.state),
        PYTHONPATH=str(PKG_ROOT / "src"),
    )
    child_env.update(env or {})
    proc = subprocess.run([sys.executable, "-c", code, *argv],
                          capture_output=True, text=True, env=child_env)
    return snapshot(out, proc.returncode, proc.stdout, proc.stderr)


@pytest.fixture
def outdir(tmp_path: Path) -> Path:
    """A fresh, empty output directory."""
    d = tmp_path / "out"
    d.mkdir()
    return d


def sequence(ds: Dataset, out: Path, runs: Sequence[Sequence[str]],
             env: Optional[Dict[str, str]] = None) -> List[Run]:
    """Replay several invocations against one output directory.

    A sequence, rather than a single call, is what exercises resume and
    warm-start behaviour: each run sees whatever the previous one left.
    """
    return [invoke(ds, out, *args, env=env) for args in runs]
