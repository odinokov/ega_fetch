"""Run the plan across a thread pool and summarise the outcome."""

from __future__ import annotations

import logging
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Sequence

from ..cancel import Canceller
from ..constants import FATAL_ERRNOS
from ..errors import Cancelled, FileFailure
from ..models import Item, Outcome
from ..util import hhmmss, human
from .converge import Converger

log = logging.getLogger(__name__)


def _failure(item: Item, exc: BaseException, cancel: Canceller) -> Outcome:
    """Map an exception from one file onto an Outcome, escalating the classes
    that will recur on every remaining file into a run-wide stop."""
    if isinstance(exc, Cancelled):
        return Outcome(item, False, "interrupted")
    if isinstance(exc, subprocess.TimeoutExpired):
        return Outcome(item, False, "timed out")
    if isinstance(exc, OSError) and exc.errno in FATAL_ERRNOS:
        log.error("%s is fatal for every remaining file; stopping the run",
                  exc.strerror or exc)
        cancel.stop()
    return Outcome(item, False, str(exc) or type(exc).__name__)


def execute(conv: Converger, pending: Sequence[Item], *, jobs: int,
            cancel: Canceller) -> List[Outcome]:
    """Converge every pending item. Every item yields an Outcome, including
    those skipped because the run is stopping — so an interrupted run is never
    mistaken for a complete one."""
    total = len(pending)
    results: List[Outcome] = []

    def work(item: Item) -> Outcome:
        if cancel.stopped:
            return Outcome(item, False, "not attempted (stopping)")
        try:
            return conv.converge(item)
        except (FileFailure, Cancelled, subprocess.TimeoutExpired, OSError) as exc:
            return _failure(item, exc, cancel)

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(work, item) for item in pending]
        for n, future in enumerate(as_completed(futures), start=1):
            res = future.result()
            results.append(res)
            if res.ok:
                rate = res.bytes_moved / res.seconds if res.seconds > 0 else 0
                log.info("[%d/%d] OK   %s  (%s in %s, %s/s)", n, total, res.item.file_name,
                         human(res.bytes_moved), hhmmss(res.seconds), human(rate))
            else:
                log.error("[%d/%d] FAIL %s: %s", n, total, res.item.file_name, res.detail)
    return results


def report(results: Sequence[Outcome], reasons: Counter[str],
           elapsed: float, cancel: Canceller) -> int:
    """Summarise the run and choose the exit code."""
    failures = [r for r in results if not r.ok]
    moved = sum(r.bytes_moved for r in results)

    log.info("-" * 64)
    log.info("converged %d | already present %d | failed %d",
             len(results) - len(failures), sum(reasons.values()), len(failures))
    if moved:
        log.info("transferred %s in %s (%s/s average)", human(moved), hhmmss(elapsed),
                 human(moved / elapsed if elapsed else 0))
    if failures:
        log.error("failed files (re-run to retry; partial transfers resume):")
        for res in failures:
            log.error("  %s: %s", res.item.file_name, res.detail)
        return 130 if cancel.stopped else 1
    return 0
