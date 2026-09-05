"""Top-level convergence entrypoint shared by the CLI and the Python API."""

from __future__ import annotations

import logging
import time

from ..cancel import Canceller
from ..constants import LEDGER_NAME, LOCK_NAME, PROG, VERSION
from ..io.csv_input import read_csv_items
from ..io.ledger import Ledger
from ..io.lock import DirLock
from ..io.sftp import Sftp
from ..models import Settings
from .converge import Converger
from .execute import execute, report
from .planning import load_metadata, plan, validate_plan

log = logging.getLogger(__name__)


def run(settings: Settings, cancel: Canceller) -> int:
    """Converge the output directory. Settings are resolved and the
    environment already checked by main()."""
    items = read_csv_items(settings.csv_path)
    settings.out_dir.mkdir(parents=True, exist_ok=True)
    log.info("%s %s | dataset=%s | %d file(s) requested | out=%s",
             PROG, VERSION, settings.dataset, len(items), settings.out_dir)

    with DirLock(settings.out_dir / LOCK_NAME):
        load_metadata(settings, items)

        sftp = Sftp(settings.remote, cancel)
        ledger = Ledger(settings.out_dir / LEDGER_NAME)
        conv = Converger(sftp, settings, ledger, cancel)

        pending, reasons = plan(conv, items, jobs=settings.jobs)
        detail = ", ".join(f"{n} {r}" for r, n in sorted(reasons.items())) or "none"
        log.info("initial state: %d converged (%s), %d pending",
                 sum(reasons.values()), detail, len(pending))
        if not pending:
            log.info("nothing to do; target state already holds")
            return 0

        log.info("connecting to %s as %s", settings.remote.host, settings.remote.username)
        sftp.connect()
        validate_plan(sftp.names(settings.dataset), pending, settings)

        if settings.dry_run:
            log.info("dry run: no files were transferred")
            return 0

        conv.staging.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        results = execute(conv, pending, jobs=settings.jobs, cancel=cancel)
        return report(results, reasons, time.monotonic() - started, cancel)
