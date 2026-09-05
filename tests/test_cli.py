"""Smoke tests for the command-line surface and the exit-code contract."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

from conftest import Dataset, invoke


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the CLI the way the console script does."""
    return subprocess.run(
        [sys.executable, "-c",
         "import sys; from ega_fetch.cli import main; sys.exit(main())", *args],
        capture_output=True, text=True,
        # The real PATH: preflight legitimately requires sftp and crypt4gh, and
        # these tests are about the CLI contract, not about a missing binary.
        env=dict(os.environ,
                 PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
                 COLUMNS="80"),
    )


def test_version_prints_prog_and_version() -> None:
    proc = run_cli("--version")
    assert proc.returncode == 0
    assert proc.stdout.strip() == "ega_fetch 2.0.0"


def test_help_documents_the_surface() -> None:
    proc = run_cli("--help")
    assert proc.returncode == 0
    for flag in ("--csv", "--out", "--config", "--username", "--key", "--dataset",
                 "--jobs", "--io-timeout", "--no-checksums", "--recheck",
                 "--refresh-manifest", "--keep-encrypted",
                 "--dry-run", "--log-file"):
        assert flag in proc.stdout, f"{flag} missing from --help"
    assert "Live Outbox" in proc.stdout


def test_missing_arguments_exit_2() -> None:
    proc = run_cli()
    assert proc.returncode == 2
    assert "--csv is required" in proc.stderr


def test_console_script_is_installed() -> None:
    """The ``ega-fetch`` entry point resolves and reports the same version."""
    candidate = Path(sys.executable).parent / "ega-fetch"
    script = str(candidate) if candidate.exists() else shutil.which("ega-fetch")
    if script is None:
        pytest.skip("console script not installed in this environment")
    proc = subprocess.run([script, "--version"], capture_output=True, text=True)
    assert proc.returncode == 0
    assert proc.stdout.strip() == "ega_fetch 2.0.0"


def test_unusable_private_key_fails_preflight(tmp_path: Path) -> None:
    """A corrupt key at correct permissions still reaches the ssh-keygen probe."""
    key = tmp_path / "bogus"
    key.write_text("not a key")
    key.chmod(0o600)
    proc = run_cli("--csv", "x.csv", "--out", str(tmp_path / "o"),
                   "--username", "u", "--key", str(key), "--dataset", "D")
    assert proc.returncode == 3
    assert "is unusable" in proc.stderr


def test_loose_key_permissions_are_refused_and_the_key_is_left_alone(
    tmp_path: Path,
) -> None:
    """A 0644 key fails preflight, and the tool never changes its mode."""
    key = tmp_path / "id_ed25519"
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key), "-q"],
                   check=True)
    key.chmod(0o644)
    args: List[str] = ["--csv", str(tmp_path / "absent.csv"), "--out", str(tmp_path / "o"),
                       "--username", "u", "--key", str(key), "--dataset", "D"]

    refused = run_cli(*args)
    assert refused.returncode == 3
    assert "group/world accessible" in refused.stderr
    assert f"chmod 600 {key}" in refused.stderr, "must say exactly what to run"
    assert key.stat().st_mode & 0o777 == 0o644, "the key must never be modified"
    # One diagnosis, not two: ssh-keygen would only restate the same problem
    # as a multi-line ASCII banner and bury the actionable sentence.
    assert "is unusable" not in refused.stderr
    assert "UNPROTECTED PRIVATE KEY FILE" not in refused.stderr
    assert refused.stderr.count("  - ") == 1

    # Fixing it by hand -- the only way -- gets past preflight.
    key.chmod(0o600)
    passed = run_cli(*args)
    assert passed.returncode == 2          # past preflight, now the missing CSV
    assert "CSV not found" in passed.stderr
    assert key.stat().st_mode & 0o777 == 0o600


def test_log_file_receives_the_same_records(dataset: Dataset, tmp_path: Path) -> None:
    """--log-file mirrors stderr to disk."""
    out = tmp_path / "out"
    out.mkdir()
    logfile = tmp_path / "run.log"
    run = invoke(dataset, out, "--log-file", str(logfile))
    assert run.code == 0
    text = logfile.read_text()
    assert "converged 3 | already present 0 | failed 0" in text
    for f in dataset.files:
        assert f.name in text


def test_quiet_suppresses_info_but_keeps_errors(dataset: Dataset, tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    run = invoke(dataset, out, "--quiet")
    assert run.code == 0
    assert run.stderr == "", "--quiet must emit nothing on a clean run"


def test_verbose_adds_debug_records(dataset: Dataset, tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    invoke(dataset, out)                       # populate the cache
    second = invoke(dataset, out, "--verbose", "--recheck")
    assert second.code == 0
    assert "using cached manifest" in second.stderr


def test_lock_contention_exits_4(dataset: Dataset, tmp_path: Path) -> None:
    """A second run against a held output directory exits 4, not 1 or 2."""
    import fcntl

    out = tmp_path / "out"
    out.mkdir()
    lock = out / ".ega_fetch.lock"
    with lock.open("w") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        run = invoke(dataset, out)
    assert run.code == 4
    assert "holds the lock on this output directory" in run.stderr
