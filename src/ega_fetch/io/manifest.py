"""Fetch, cache and parse the archive's published checksums and sizes.

Purely factual: this module answers "what does the archive publish?" and
holds no run policy. What to do when the answer is unavailable belongs to
:mod:`ega_fetch.core.planning`.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Sequence, cast

from ..constants import MANIFEST_NAME, MANIFEST_VERSION, METADATA_BASE, PROG, VERSION
from ..errors import PreflightError
from ..models import FileMeta
from ..util import atomic_write_json, now_stamp, read_json

log = logging.getLogger(__name__)


def http_get_json(url: str, timeout: int = 60) -> object:
    """GET ``url`` and decode the response as JSON."""
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": f"{PROG}/{VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _records_from(payload: object, url: str) -> Sequence[object]:
    """Unwrap the metadata response envelope, or say we could not read it."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        records = payload.get("files") or payload.get("results") or payload.get("items")
        if records is not None:
            # The envelope is untyped JSON; the element loop below tolerates
            # anything non-dict, so nothing is asserted about the members here.
            return cast(Sequence[object], records)
        raise PreflightError(
            f"unrecognised response shape from {url} "
            f"(top-level keys: {', '.join(sorted(payload)) or 'none'})"
        )
    raise PreflightError(f"unrecognised response from {url}: {type(payload).__name__}")


def _parse_manifest(payload: object, url: str, dataset: str) -> Dict[str, FileMeta]:
    """Pull accession -> (sha256, encrypted size) out of a metadata response."""
    records = _records_from(payload, url)
    meta: Dict[str, FileMeta] = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        acc = rec.get("accession_id")
        digest = rec.get("unencrypted_checksum")
        size = rec.get("filesize")
        algo = (rec.get("unencrypted_checksum_type") or "").upper()
        if acc and digest and algo == "SHA256" and isinstance(size, int):
            meta[acc] = FileMeta(sha256=digest.lower(), enc_size=size)
    if not meta:
        raise PreflightError(
            f"{dataset} publishes no SHA-256 checksums ({len(records)} record(s) read)"
        )
    return meta


def load_manifest(dataset: str, out_dir: Path, refresh: bool) -> Dict[str, FileMeta]:
    """accession -> FileMeta. Cached on disk; refetched on a schema bump."""
    cache = out_dir / MANIFEST_NAME
    if not refresh:
        cached = read_json(cache, default={})
        if isinstance(cached, dict) and cached.get("version") == MANIFEST_VERSION \
                and cached.get("dataset") == dataset and cached.get("files"):
            log.debug("using cached manifest (%d entries)", len(cached["files"]))
            return {a: FileMeta(**m) for a, m in cached["files"].items()}

    url = f"{METADATA_BASE}/datasets/{dataset}/files?limit=100000"
    log.info("fetching manifest from %s", url)
    try:
        payload = http_get_json(url)
    except (urllib.error.URLError, ValueError, TimeoutError, OSError) as exc:
        raise PreflightError(f"could not fetch the manifest for {dataset}: {exc}") from exc

    meta = _parse_manifest(payload, url, dataset)
    atomic_write_json(cache, {
        "version": MANIFEST_VERSION, "dataset": dataset, "fetched_at": now_stamp(),
        "files": {a: dataclasses.asdict(m) for a, m in meta.items()},
    })
    log.info("manifest: %d entries", len(meta))
    return meta
