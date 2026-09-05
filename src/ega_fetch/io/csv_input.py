"""Read the input CSV into the list of files to converge."""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..errors import ConfigError
from ..models import Item

log = logging.getLogger(__name__)

CsvRow = Dict[str, Any]

REQUIRED_COLUMNS = ("file_name", "file_accession_id")


def _csv_rows(csv_path: Path) -> Iterator[Tuple[int, CsvRow]]:
    """Yield (line number, row), validating the header once."""
    if not csv_path.is_file():
        raise ConfigError(f"CSV not found: {csv_path}")
    with csv_path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        cols = set(reader.fieldnames or ())
        absent = [c for c in REQUIRED_COLUMNS if c not in cols]
        if absent:
            raise ConfigError(
                f"CSV missing required column '{absent[0]}'. "
                f"Found: {', '.join(sorted(cols)) or '(none)'}"
            )
        yield from enumerate(reader, start=2)


def _item_from_row(lineno: int, row: CsvRow) -> Optional[Item]:
    """Validate one row into an :class:`~ega_fetch.models.Item`.

    Returns None for a row that is skippable (blank name or accession); raises
    :class:`~ega_fetch.errors.ConfigError` for one that is not.
    """
    name = (row.get("file_name") or "").strip()
    acc = (row.get("file_accession_id") or "").strip()
    if not name or not acc:
        log.warning("CSV line %d: blank file_name or accession; skipping", lineno)
        return None
    if "/" in name or name in (".", ".."):
        raise ConfigError(f"CSV line {lineno}: unsafe file_name {name!r}")
    return Item(file_name=name, accession=acc)


def read_csv_items(csv_path: Path) -> List[Item]:
    """Return the requested files, de-duplicated by name and sorted by it."""
    items: Dict[str, Item] = {}
    for lineno, row in _csv_rows(csv_path):
        item = _item_from_row(lineno, row)
        if item is None:
            continue
        prior = items.get(item.file_name)
        if prior and prior.accession != item.accession:
            raise ConfigError(
                f"CSV line {lineno}: {item.file_name!r} appears twice with different "
                f"accessions ({prior.accession} and {item.accession})"
            )
        items[item.file_name] = item
    if not items:
        raise ConfigError(f"no usable rows in {csv_path}")
    return sorted(items.values(), key=lambda i: i.file_name)
