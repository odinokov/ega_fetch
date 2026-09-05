"""Thin argument-parsing layer. Contains no business logic."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from typing import Optional, Sequence

from .cancel import Canceller
from .config import CONFIG_KEYS, resolve_settings
from .constants import DEFAULT_HOST, DEFAULT_IO_TIMEOUT, DEFAULT_PORT, PROG, VERSION
from .core.preflight import check_environment
from .core.run import run
from .errors import Cancelled, ConfigError, LockBusy, PreflightError

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Construct the command-line parser. ``prog`` stays ``ega_fetch``."""
    p = argparse.ArgumentParser(
        prog=PROG,
        description="Converge a directory to a verified copy of an EGA dataset "
                    "via Live Outbox (SFTP + Crypt4GH).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  # first run, with a config file holding username and key path
  %(prog)s --csv sample_file.csv --config ega.json --out /data/ega

  # everything on the command line
  %(prog)s --csv sample_file.csv --out /data/ega \\
      --username you@inst.edu --key ~/.ssh/id_ed25519 --dataset EGAD50000000695

  # see what would happen, transfer nothing
  %(prog)s --csv sample_file.csv --config ega.json --out /data/ega --dry-run

  # prove the pipeline on one file: give it a CSV holding just that row
  %(prog)s --csv one_file.csv --config ega.json --out /data/ega

config file (JSON) — every setting below may be given here instead:
  {"username": "you@inst.edu", "private_key": "~/.ssh/id_ed25519",
   "dataset": "EGAD50000000695", "jobs": 4, "io_timeout": 7200}

Re-running is always safe: verified files are skipped and partial
transfers resume where they stopped.
""",
    )
    p.add_argument("--csv", metavar="PATH",
                   help="CSV with columns file_name and file_accession_id")
    p.add_argument("--out", metavar="DIR", help="output directory (created if absent)")
    p.add_argument("--config", metavar="PATH",
                   help="JSON file supplying any of: " + ", ".join(sorted(CONFIG_KEYS)))
    p.add_argument("--username", metavar="USER", help="EGA account name (may contain '@')")
    p.add_argument("--key", metavar="PATH", help="ssh private key registered with EGA")
    p.add_argument("--dataset", metavar="EGAD...", help="dataset accession")
    p.add_argument("--host", metavar="HOST", help=f"outbox host (default {DEFAULT_HOST})")
    p.add_argument("--port", type=int, metavar="N", help=f"ssh port (default {DEFAULT_PORT})")

    p.add_argument("-j", "--jobs", type=int, metavar="N",
                   help="concurrent file transfers (default 1)")
    p.add_argument("--io-timeout", type=int, metavar="SEC",
                   help=f"per-file transfer timeout (default {DEFAULT_IO_TIMEOUT}s)")

    # Flags default to None so 'absent' is distinguishable from 'set false',
    # which is what lets the config file supply them too.
    p.add_argument("--no-checksums", dest="verify_checksums", action="store_false",
                   default=None, help="transfer without verifying SHA-256 (not recommended)")
    p.add_argument("--recheck", action="store_true", default=None,
                   help="ignore the ledger and re-verify every existing file")
    p.add_argument("--refresh-manifest", action="store_true", default=None,
                   help="refetch the manifest instead of using the cache")
    p.add_argument("--keep-encrypted", action="store_true", default=None,
                   help="retain the .c4gh files in staging after decryption")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="report the plan and exit without transferring")

    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("-q", "--quiet", action="store_true", help="warnings and errors only")
    p.add_argument("--log-file", metavar="PATH", help="also append logs to this file")
    p.add_argument("--version", action="version", version=f"{PROG} {VERSION}")
    return p


def setup_logging(args: argparse.Namespace) -> None:
    """Attach a stderr handler, and a file handler when ``--log-file`` is given."""
    level = logging.DEBUG if args.verbose else logging.WARNING if args.quiet else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger(PROG)
    root.setLevel(level)
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    root.addHandler(stream)
    if args.log_file:
        fh = logging.FileHandler(args.log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse arguments, install signal handlers, and converge. Returns the exit code."""
    args = build_parser().parse_args(argv)
    setup_logging(args)

    cancel = Canceller()

    def on_signal(signum: int, _frame: object) -> None:
        if not cancel.stopped:
            log.warning("signal %d received; stopping in-flight transfers "
                        "(partial files resume on the next run)", signum)
            cancel.stop()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    try:
        settings = resolve_settings(args)
        check_environment(settings.remote)
        return run(settings, cancel)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    except PreflightError as exc:
        log.error("preflight failed:\n%s", exc.render())
        return 3
    except LockBusy as exc:
        log.error("%s", exc)
        return 4
    except (Cancelled, KeyboardInterrupt):
        log.error("interrupted")
        return 130
