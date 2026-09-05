"""Decide which files still need work, and prove the plan can run."""

from __future__ import annotations

import logging
import shutil
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import List, Sequence, Set, Tuple

from ..errors import PreflightError
from ..io.manifest import load_manifest
from ..models import Item, Settings
from ..util import human, preview
from .converge import Converger

log = logging.getLogger(__name__)


def plan(conv: Converger, items: Sequence[Item], *, jobs: int
         ) -> Tuple[List[Item], Counter[str]]:
    """Classify items into the work to do. Returns (pending, reasons).

    Classification is spread over the pool because a cold ledger means hashing
    every file already on disk, which is the one expensive case.
    """
    reasons: Counter[str] = Counter()
    pending: List[Item] = []
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for decision in pool.map(conv.classify, items):
            if decision.converged:
                reasons[decision.reason] += 1
            else:
                pending.append(decision.item)
    return pending, reasons


def validate_plan(names: Set[str], pending: Sequence[Item], s: Settings) -> None:
    """Prove the planned files are staged remotely and will fit on disk."""
    absent = [i.file_name for i in pending if i.enc_name not in names]
    if absent:
        raise PreflightError(
            f"{len(absent)} planned file(s) are absent from {s.dataset} "
            f"on the outbox: {preview(absent)}"
        )

    sizes = [i.enc_size for i in pending if i.enc_size is not None]
    free = shutil.disk_usage(s.out_dir).free
    if sizes and not s.dry_run:
        # Each worker holds one .c4gh (counted in the sum) plus the plaintext
        # it is writing, so headroom scales with concurrency, not with 1.
        need = sum(sizes) + max(sizes) * s.jobs
        if free < need:
            raise PreflightError(
                f"insufficient space in {s.out_dir}: {human(free)} free, "
                f"{human(need)} needed (encrypted, plus headroom to decrypt "
                f"{s.jobs} file(s) at once)"
            )
    log.info("plan: %d file(s), %s encrypted; %s free in %s", len(pending),
             human(sum(sizes)) if sizes else "unknown", human(free), s.out_dir)


def load_metadata(s: Settings, items: Sequence[Item]) -> None:
    """Attach published checksums and encrypted sizes to items.

    The manifest is fetched even under --no-checksums, because it also carries
    the sizes used for the resume test and the space budget; --no-checksums
    means 'do not block on verification', not 'do not look'.
    """
    try:
        meta = load_manifest(s.dataset, s.out_dir, refresh=s.refresh_manifest)
    except PreflightError:
        if s.verify:
            raise
        log.warning("manifest unavailable; continuing without checksums or size checks")
        return

    unknown = [i.file_name for i in items if i.accession not in meta]
    if unknown and s.verify:
        raise PreflightError(
            f"no published checksum for {len(unknown)} file(s): {preview(unknown, 3)}",
            hint="Use --no-checksums to transfer without verification.",
        )
    for item in items:
        entry = meta.get(item.accession)
        if entry:
            item.sha256 = entry.sha256
            item.enc_size = entry.enc_size
