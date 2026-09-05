"""Resolve settings from the command line over the JSON config file."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional

from .constants import DEFAULT_HOST, DEFAULT_IO_TIMEOUT, DEFAULT_PORT
from .errors import ConfigError
from .models import Remote, Settings

log = logging.getLogger(__name__)

# argparse dest -> config-file key, where the two names differ.
CONFIG_ALIASES = {"key": "private_key", "csv": "csv_path", "out": "out_dir"}
# Every setting is addressable from the config file; none is CLI-only.
CONFIG_KEYS: FrozenSet[str] = frozenset({
    "csv_path", "out_dir", "username", "private_key", "host", "port", "dataset",
    "jobs", "io_timeout", "verify_checksums", "recheck",
    "refresh_manifest", "keep_encrypted",
})


def load_config(path: Optional[Path]) -> Dict[str, Any]:
    """Read the user's config file. Unlike a cache, every failure here is
    something the user must see, reported as what actually went wrong."""
    if path is None:
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc.strerror or exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ConfigError(f"config file {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config file must contain a JSON object: {path}")
    unknown = set(data) - CONFIG_KEYS
    if unknown:
        log.warning("ignoring unknown config keys: %s", ", ".join(sorted(unknown)))
    return data


def _value(args: argparse.Namespace, cfg: Dict[str, Any], dest: str,
           default: Any = None) -> Any:
    """CLI wins over config file, config file over the built-in default.

    Raises AttributeError on a name that is not an argparse dest, so a typo
    fails loudly instead of silently falling through to the default.

    Returns ``Any`` deliberately. An ``argparse.Namespace`` attribute joined to
    an arbitrary JSON value is the one genuinely untyped seam in this program;
    every caller below narrows it immediately with ``str``/``int``/``Path``,
    exactly as the original did.
    """
    value = getattr(args, dest)
    if value is not None:
        return value
    return cfg.get(CONFIG_ALIASES.get(dest, dest), default)


def _flag(args: argparse.Namespace, cfg: Dict[str, Any], dest: str, default: bool) -> bool:
    """Same precedence for boolean flags. Their argparse default is None, so
    'not given on the command line' is distinguishable from 'given as false'."""
    value = _value(args, cfg, dest)
    return default if value is None else bool(value)


def resolve_settings(args: argparse.Namespace) -> Settings:
    """Merge CLI over config file into one frozen record."""
    cfg = load_config(Path(args.config).expanduser() if args.config else None)
    required = {dest: _value(args, cfg, dest)
                for dest in ("csv", "out", "username", "key", "dataset")}
    for dest, value in required.items():
        if not value:
            raise ConfigError(
                f"--{dest} is required (give it on the command line or in --config)"
            )
    remote = Remote(
        host=_value(args, cfg, "host", DEFAULT_HOST),
        port=int(_value(args, cfg, "port", DEFAULT_PORT)),
        username=required["username"],
        key_path=Path(required["key"]).expanduser(),
    )
    return Settings(
        csv_path=Path(required["csv"]).expanduser(),
        out_dir=Path(required["out"]).expanduser().resolve(),
        remote=remote,
        dataset=required["dataset"],
        c4gh_key=remote.key_path,   # on EGA the ssh identity is also the c4gh key
        jobs=max(1, int(_value(args, cfg, "jobs", 1))),
        io_timeout=int(_value(args, cfg, "io_timeout", DEFAULT_IO_TIMEOUT)),
        verify=_flag(args, cfg, "verify_checksums", True),
        recheck=_flag(args, cfg, "recheck", False),
        refresh_manifest=_flag(args, cfg, "refresh_manifest", False),
        keep_encrypted=_flag(args, cfg, "keep_encrypted", False),
        dry_run=bool(args.dry_run),
    )
