"""End-to-end behaviour of the CLI against a synthetic dataset.

These eleven scenarios began life as a differential test that ran the original
single-file ``ega_fetch.py`` and this package side by side and asserted the two
were indistinguishable. That port is complete and was verified 12/12 at the
time; the assertions here are the same scenarios restated against this package
alone, so the coverage survives the original's removal.

Everything asserted is exact: digests, byte counts, exit codes and ledger
contents are all discrete, so no tolerance appears anywhere.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from conftest import Dataset, invoke, sequence


def test_cold_run_converges_every_file(dataset: Dataset, outdir: Path) -> None:
    """From an empty directory: transfer, decrypt, verify and publish all files."""
    run = invoke(dataset, outdir)

    assert run.code == 0
    assert set(run.files) == {f.name for f in dataset.files}
    for f in dataset.files:
        assert run.files[f.name] == f.sha256, f"{f.name} content differs"
    assert run.staging == [], "staging should be empty once files are published"
    assert run.ledger is not None
    assert set(run.ledger["files"]) == {f.name for f in dataset.files}


def test_published_bytes_match_the_plaintext_exactly(
    dataset: Dataset, outdir: Path
) -> None:
    """Not just equal digests: the decrypted file is the original plaintext."""
    run = invoke(dataset, outdir)
    assert run.code == 0
    for f in dataset.files:
        assert (outdir / f.name).read_bytes() == f.plain

    ledger = json.loads((outdir / ".ega_state.json").read_text())
    for f in dataset.files:
        entry = ledger["files"][f.name]
        assert entry["sha256"] == f.sha256
        assert entry["size"] == len(f.plain)
        assert entry["accession"] == f.accession


def test_warm_run_is_a_no_op(dataset: Dataset, outdir: Path) -> None:
    """A second run trusts the ledger, transfers nothing, and still exits 0."""
    cold, warm = sequence(dataset, outdir, [(), ()])

    assert warm.code == 0
    assert warm.files == cold.files
    assert "nothing to do; target state already holds" in warm.stderr
    assert "3 converged (3 ledger)" in warm.stderr


def test_resume_after_a_truncated_transfer(dataset: Dataset, outdir: Path) -> None:
    """A partial .c4gh is resumed byte-exactly rather than refetched."""
    first, second = sequence(dataset, outdir, [(), ()],
                             env={"FAKE_SFTP_FIRST_CHUNK": "5000"})

    assert first.code == 1, "a truncated transfer must fail, not publish"
    assert "size mismatch after transfer" in first.stderr

    assert second.code == 0
    assert "resuming at" in second.stderr
    for f in dataset.files:
        assert second.files[f.name] == f.sha256


def test_dry_run_transfers_nothing(dataset: Dataset, outdir: Path) -> None:
    """--dry-run reports the plan, writes no data file, and exits 0."""
    run = invoke(dataset, outdir, "--dry-run")

    assert run.code == 0
    assert run.files == {}
    assert "dry run: no files were transferred" in run.stderr


def test_checksum_mismatch_is_not_published(dataset: Dataset, outdir: Path) -> None:
    """A file whose digest disagrees with the manifest never reaches the output."""
    import conftest

    poisoned = dataset.records()
    poisoned[0]["unencrypted_checksum"] = "0" * 64
    conftest._Handler.records = poisoned

    run = invoke(dataset, outdir)

    assert run.code == 1
    assert "SHA-256 mismatch" in run.stderr
    assert dataset.files[0].name not in run.files
    assert len(run.files) == len(dataset.files) - 1


def test_keep_encrypted_retains_staging(dataset: Dataset, outdir: Path) -> None:
    """--keep-encrypted leaves every .c4gh behind and still publishes correctly."""
    run = invoke(dataset, outdir, "--keep-encrypted")

    assert run.code == 0
    assert run.staging == sorted(f.name + ".c4gh" for f in dataset.files)
    for f in dataset.files:
        assert run.files[f.name] == f.sha256


def test_recheck_rehashes_instead_of_trusting_the_ledger(
    dataset: Dataset, outdir: Path
) -> None:
    """--recheck ignores the ledger and verifies the bytes on disk again."""
    _, rechecked = sequence(dataset, outdir, [(), ("--recheck",)])

    assert rechecked.code == 0
    assert "3 rehashed" in rechecked.stderr


def test_recheck_detects_same_size_corruption(dataset: Dataset, outdir: Path) -> None:
    """--recheck catches bit rot that leaves the file size unchanged.

    The size-changing case needs no flag: the ledger records the size, so a
    truncated or overwritten file is already refused on an ordinary run. What
    only --recheck can catch is corruption that preserves the size, which is
    what this asserts -- first that a normal run is fooled by it, then that
    --recheck is not.
    """
    target = dataset.files[1]
    assert invoke(dataset, outdir).code == 0

    victim = outdir / target.name
    original = victim.read_bytes()
    rotted = b"X" * len(original)
    assert len(rotted) == len(original) and rotted != original

    # An ordinary run trusts the ledger: same size, so it never re-reads.
    victim.write_bytes(rotted)
    trusting = invoke(dataset, outdir)
    assert trusting.code == 0
    assert "3 ledger" in trusting.stderr
    assert victim.read_bytes() == rotted, "the cheap path is expected to be fooled here"

    # --recheck re-hashes, notices, and re-fetches.
    repaired = invoke(dataset, outdir, "--recheck")
    assert repaired.code == 0
    assert "checksum differs from the manifest" in repaired.stderr
    assert victim.read_bytes() == original
    assert hashlib.sha256(victim.read_bytes()).hexdigest() == target.sha256


def test_size_changing_corruption_is_caught_without_recheck(
    dataset: Dataset, outdir: Path
) -> None:
    """A truncated file is refused on an ordinary run -- the ledger stores size."""
    assert invoke(dataset, outdir).code == 0

    target = dataset.files[1]
    victim = outdir / target.name
    victim.write_bytes(b"short")

    run = invoke(dataset, outdir)
    assert run.code == 0
    assert hashlib.sha256(victim.read_bytes()).hexdigest() == target.sha256


def test_no_checksums_still_publishes(dataset: Dataset, outdir: Path) -> None:
    """--no-checksums transfers without blocking on verification."""
    run = invoke(dataset, outdir, "--no-checksums")

    assert run.code == 0
    assert set(run.files) == {f.name for f in dataset.files}


def test_login_failure_is_a_preflight_error(dataset: Dataset, outdir: Path) -> None:
    """A rejected key fails preflight, before anything is transferred."""
    run = invoke(dataset, outdir, env={"FAKE_SFTP_FAIL_LOGIN": "1"})

    assert run.code == 3
    assert "cannot log in" in run.stderr
    assert run.files == {}


def test_manifest_cache_is_written_and_reused(dataset: Dataset, outdir: Path) -> None:
    """The manifest is cached on the first run and not refetched on the second."""
    cold, warm = sequence(dataset, outdir, [(), ()])

    assert cold.manifest is not None
    assert cold.manifest["version"] == 2
    assert cold.manifest["dataset"] == "EGAD50000000000"
    assert "fetching manifest from" in cold.stderr
    assert "fetching manifest from" not in warm.stderr
