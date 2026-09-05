"""Unit tests for the pieces that can be exercised without a transfer."""

from __future__ import annotations

import argparse
import errno
import hashlib
import io
import json
import subprocess
import threading
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

import pytest

from ega_fetch.cancel import Canceller
from ega_fetch.config import _flag, _value, load_config, resolve_settings
from ega_fetch.constants import IO_CHUNK
from ega_fetch.core.execute import _failure, report
from ega_fetch.errors import Cancelled, ConfigError, FileFailure, LockBusy, PreflightError
from ega_fetch.io.csv_input import read_csv_items
from ega_fetch.io.ledger import Ledger
from ega_fetch.io.lock import DirLock
from ega_fetch.io.manifest import _parse_manifest, _records_from
from ega_fetch.models import Item, Outcome, Remote
from ega_fetch.util import (
    atomic_write_json,
    hash_stream,
    hhmmss,
    human,
    preview,
    read_json,
    sha256_file,
    size_or_zero,
    unlink_quiet,
)

# --- formatting -------------------------------------------------------------

@pytest.mark.parametrize(("value", "expected"), [
    (0, "0.0 B"), (1023, "1023.0 B"), (1024, "1.0 KB"),
    (1024 ** 2, "1.0 MB"), (1024 ** 3, "1.0 GB"),
    (1024 ** 4, "1.0 TB"), (1024 ** 5, "1.0 PB"),
])
def test_human_uses_binary_units(value: int, expected: str) -> None:
    assert human(value) == expected


@pytest.mark.parametrize(("seconds", "expected"), [
    (0, "0m00s"), (-5, "0m00s"), (59, "0m59s"),
    (60, "1m00s"), (3599, "59m59s"), (3600, "1h00m00s"), (7325, "2h02m05s"),
])
def test_hhmmss_omits_hours_when_zero(seconds: int, expected: str) -> None:
    assert hhmmss(seconds) == expected


def test_preview_elides_the_tail() -> None:
    assert preview(["a", "b"]) == "a, b"
    assert preview([str(i) for i in range(8)]) == "0, 1, 2, 3, 4 (+3 more)"
    assert preview(["a", "b", "c"], 1) == "a (+2 more)"


# --- filesystem helpers -----------------------------------------------------

def test_size_or_zero_treats_absent_as_empty(tmp_path: Path) -> None:
    assert size_or_zero(tmp_path / "nope") == 0
    target = tmp_path / "f"
    target.write_bytes(b"1234")
    assert size_or_zero(target) == 4
    assert size_or_zero(tmp_path) >= 0  # a directory must not raise


def test_unlink_quiet_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "f"
    target.write_text("x")
    unlink_quiet(target)
    unlink_quiet(target)          # already gone: must not raise
    unlink_quiet(tmp_path)        # a directory: must not raise
    assert not target.exists()


# --- hashing ----------------------------------------------------------------

def test_hash_stream_matches_hashlib_and_tees() -> None:
    payload = b"abc" * 1000
    sink = io.BytesIO()
    digest, total = hash_stream(io.BytesIO(payload), sink)
    assert digest == hashlib.sha256(payload).hexdigest()
    assert total == len(payload)
    assert sink.getvalue() == payload


def test_hash_stream_spans_multiple_chunks() -> None:
    payload = b"\xa5" * (IO_CHUNK * 2 + 17)
    digest, total = hash_stream(io.BytesIO(payload))
    assert total == len(payload)
    assert digest == hashlib.sha256(payload).hexdigest()


def test_hash_stream_honours_cancellation() -> None:
    cancel = Canceller()
    cancel.stop()
    with pytest.raises(Cancelled):
        hash_stream(io.BytesIO(b"x" * 10), cancel=cancel)


def test_sha256_file_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "f"
    target.write_bytes(b"payload")
    assert sha256_file(target) == hashlib.sha256(b"payload").hexdigest()


# --- JSON -------------------------------------------------------------------

def test_read_json_is_lossy_for_caches(tmp_path: Path) -> None:
    assert read_json(tmp_path / "absent", default={"d": 1}) == {"d": 1}
    empty = tmp_path / "empty"
    empty.write_text("   ")
    assert read_json(empty, default=None) is None
    broken = tmp_path / "broken"
    broken.write_text("{not json")
    assert read_json(broken, default=[]) == []
    good = tmp_path / "good"
    good.write_text('{"a": 1}')
    assert read_json(good) == {"a": 1}


def test_atomic_write_json_leaves_no_temp_file(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "state.json"
    atomic_write_json(target, {"b": 2, "a": 1})
    assert json.loads(target.read_text()) == {"a": 1, "b": 2}
    assert [p.name for p in target.parent.iterdir()] == ["state.json"]


def test_atomic_write_json_cleans_up_on_failure(tmp_path: Path) -> None:
    class Unserialisable:
        pass

    with pytest.raises(TypeError):
        atomic_write_json(tmp_path / "x.json", {"bad": Unserialisable()})
    assert list(tmp_path.iterdir()) == [], "a failed write must leave nothing behind"


# --- models -----------------------------------------------------------------

def test_item_derives_the_encrypted_name() -> None:
    assert Item("a.cram", "EGAF1").enc_name == "a.cram.c4gh"


def test_ssh_opts_carry_user_and_port() -> None:
    opts = Remote("h", 2222, "me@inst.edu", Path("/k")).ssh_opts()
    assert "User=me@inst.edu" in opts
    assert "Port=2222" in opts
    assert opts[-2:] == ["-i", "/k"]
    assert "BatchMode=yes" in opts


def test_preflight_error_renders_bullets_and_hint() -> None:
    err = PreflightError("first", "", "second", hint="try this")
    assert err.problems == ["first", "second"]
    assert err.render() == "  - first\n  - second\n    try this"
    assert str(err) == "first; second"


# --- CSV --------------------------------------------------------------------

def _csv(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "in.csv"
    path.write_text(body)
    return path


def test_read_csv_items_dedupes_and_sorts(tmp_path: Path) -> None:
    items = read_csv_items(_csv(tmp_path,
        "file_name,file_accession_id\nb.cram,EGAF2\na.cram,EGAF1\na.cram,EGAF1\n"))
    assert [i.file_name for i in items] == ["a.cram", "b.cram"]


def test_read_csv_items_skips_blank_rows(tmp_path: Path) -> None:
    items = read_csv_items(_csv(tmp_path,
        "file_name,file_accession_id\n,EGAF1\nb.cram,\nc.cram,EGAF3\n"))
    assert [i.file_name for i in items] == ["c.cram"]


@pytest.mark.parametrize(("body", "fragment"), [
    ("name,acc\nx,y\n", "missing required column 'file_name'"),
    ("file_name,file_accession_id\n../evil,EGAF1\n", "unsafe file_name"),
    ("file_name,file_accession_id\n/abs,EGAF1\n", "unsafe file_name"),
    ("file_name,file_accession_id\na.cram,EGAF1\na.cram,EGAF9\n",
     "appears twice with different accessions"),
    ("file_name,file_accession_id\n", "no usable rows"),
])
def test_read_csv_items_rejects_bad_input(tmp_path: Path, body: str, fragment: str) -> None:
    with pytest.raises(ConfigError, match=fragment):
        read_csv_items(_csv(tmp_path, body))


def test_read_csv_items_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="CSV not found"):
        read_csv_items(tmp_path / "absent.csv")


# --- config -----------------------------------------------------------------

def test_load_config_reports_what_went_wrong(tmp_path: Path) -> None:
    assert load_config(None) == {}
    with pytest.raises(ConfigError, match="cannot read config file"):
        load_config(tmp_path / "absent.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{oops")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_config(bad)
    notobj = tmp_path / "list.json"
    notobj.write_text("[1, 2]")
    with pytest.raises(ConfigError, match="must contain a JSON object"):
        load_config(notobj)


def test_load_config_warns_about_unknown_keys(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "c.json"
    path.write_text('{"jobs": 2, "typo": 1}')
    with caplog.at_level("WARNING"):
        assert load_config(path) == {"jobs": 2, "typo": 1}
    assert "ignoring unknown config keys: typo" in caplog.text


def _args(**kw: Any) -> argparse.Namespace:
    defaults: Dict[str, Any] = {
        "csv": None, "out": None, "config": None, "username": None, "key": None,
        "dataset": None, "host": None, "port": None, "jobs": None,
        "io_timeout": None, "verify_checksums": None, "recheck": None,
        "refresh_manifest": None, "keep_encrypted": None, "dry_run": False,
    }
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def test_value_precedence_is_cli_then_config_then_default() -> None:
    args = _args(jobs=8)
    assert _value(args, {"jobs": 4}, "jobs", 1) == 8
    assert _value(_args(), {"jobs": 4}, "jobs", 1) == 4
    assert _value(_args(), {}, "jobs", 1) == 1


def test_value_uses_the_config_alias() -> None:
    assert _value(_args(), {"private_key": "/k"}, "key") == "/k"
    assert _value(_args(), {"csv_path": "/c"}, "csv") == "/c"
    assert _value(_args(), {"out_dir": "/o"}, "out") == "/o"


def test_value_raises_on_a_dest_typo() -> None:
    with pytest.raises(AttributeError):
        _value(_args(), {}, "jbos")


def test_flag_distinguishes_absent_from_false() -> None:
    assert _flag(_args(), {}, "recheck", False) is False
    assert _flag(_args(recheck=True), {}, "recheck", False) is True
    assert _flag(_args(), {"recheck": True}, "recheck", False) is True
    assert _flag(_args(verify_checksums=False), {}, "verify_checksums", True) is False


def test_resolve_settings_requires_the_five_essentials() -> None:
    with pytest.raises(ConfigError, match="--csv is required"):
        resolve_settings(_args())


def test_resolve_settings_merges_config_and_cli(tmp_path: Path) -> None:
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({
        "username": "me@inst.edu", "private_key": str(tmp_path / "k"),
        "dataset": "EGAD1", "jobs": 4, "io_timeout": 60, "keep_encrypted": True,
    }))
    settings = resolve_settings(_args(
        csv=str(tmp_path / "in.csv"), out=str(tmp_path / "out"),
        config=str(cfg), jobs=9,
    ))
    assert settings.jobs == 9                      # CLI wins
    assert settings.io_timeout == 60               # config supplies
    assert settings.keep_encrypted is True         # boolean from config
    assert settings.verify is True                 # built-in default
    assert settings.dataset == "EGAD1"
    assert settings.out_dir.is_absolute()
    assert settings.c4gh_key == settings.remote.key_path


def test_resolve_settings_floors_jobs_at_one(tmp_path: Path) -> None:
    settings = resolve_settings(_args(
        csv="c", out=str(tmp_path), username="u", key="k", dataset="D", jobs=0))
    assert settings.jobs == 1


# --- ledger -----------------------------------------------------------------

def test_ledger_round_trips_through_disk(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    ledger = Ledger(path)
    assert ledger.get("a.cram") is None

    item = Item("a.cram", "EGAF1", sha256="d" * 64)
    ledger.record(item, 42, "d" * 64)
    entry = ledger.get("a.cram")
    assert entry is not None
    assert entry["size"] == 42
    assert entry["accession"] == "EGAF1"

    reloaded = Ledger(path)
    assert reloaded.get("a.cram") == entry

    reloaded.forget("a.cram")
    assert reloaded.get("a.cram") is None
    assert Ledger(path).get("a.cram") is None


def test_ledger_get_returns_a_copy(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "s.json")
    ledger.record(Item("a", "EGAF1"), 1, "x")
    entry = ledger.get("a")
    assert entry is not None
    entry["size"] = 999
    second = ledger.get("a")
    assert second is not None
    assert second["size"] == 1


def test_ledger_survives_a_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text("{ not json")
    assert Ledger(path).get("anything") is None


# --- lock -------------------------------------------------------------------

def test_dirlock_is_exclusive_and_self_cleaning(tmp_path: Path) -> None:
    path = tmp_path / "d" / "run.lock"
    with DirLock(path) as held:
        assert held.path == path
        assert path.is_file()
        assert "pid=" in path.read_text()
        with pytest.raises(LockBusy, match="another ega_fetch run holds the lock"), \
                DirLock(path):
            pass
    assert not path.exists()
    with DirLock(path):        # reacquirable once released
        pass


# --- cancellation -----------------------------------------------------------

def test_canceller_starts_clear() -> None:
    cancel = Canceller()
    assert not cancel.stopped
    cancel.check()          # must not raise


def test_canceller_stops_and_reaps_children() -> None:
    cancel = Canceller()
    proc = subprocess.Popen(["sleep", "30"])
    with cancel.child(proc):
        cancel.stop()
        assert proc.wait(timeout=10) != 0
    assert cancel.stopped
    with pytest.raises(Cancelled):
        cancel.check()


def test_canceller_terminates_a_child_registered_after_stop() -> None:
    cancel = Canceller()
    cancel.stop()
    proc = subprocess.Popen(["sleep", "30"])
    with cancel.child(proc):
        pass
    assert proc.wait(timeout=10) != 0


def test_canceller_is_thread_safe() -> None:
    cancel = Canceller()
    seen: List[bool] = []

    def worker() -> None:
        try:
            for _ in range(1000):
                cancel.check()
        except Cancelled:
            seen.append(True)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    cancel.stop()
    for t in threads:
        t.join(timeout=10)
    assert cancel.stopped


# --- manifest parsing -------------------------------------------------------

def test_records_from_accepts_the_known_envelopes() -> None:
    assert _records_from([1, 2], "u") == [1, 2]
    for key in ("files", "results", "items"):
        assert _records_from({key: [7]}, "u") == [7]


def test_records_from_rejects_the_unknown() -> None:
    with pytest.raises(PreflightError, match="unrecognised response shape"):
        _records_from({"nope": 1}, "u")
    with pytest.raises(PreflightError, match="unrecognised response from"):
        _records_from("a string", "u")


def test_parse_manifest_keeps_only_complete_sha256_records() -> None:
    payload = [
        {"accession_id": "EGAF1", "unencrypted_checksum": "AB" * 32,
         "unencrypted_checksum_type": "sha256", "filesize": 10},
        {"accession_id": "EGAF2", "unencrypted_checksum": "cd" * 32,
         "unencrypted_checksum_type": "MD5", "filesize": 10},      # wrong algorithm
        {"accession_id": "EGAF3", "unencrypted_checksum": "ef" * 32,
         "unencrypted_checksum_type": "SHA256"},                   # no size
        "not a dict",
    ]
    meta = _parse_manifest(payload, "u", "EGAD1")
    assert set(meta) == {"EGAF1"}
    assert meta["EGAF1"].sha256 == "ab" * 32       # normalised to lower case
    assert meta["EGAF1"].enc_size == 10


def test_parse_manifest_rejects_a_dataset_with_no_checksums() -> None:
    with pytest.raises(PreflightError, match="publishes no SHA-256 checksums"):
        _parse_manifest([{"accession_id": "EGAF1"}], "u", "EGAD1")


# --- outcome reporting ------------------------------------------------------

def test_report_chooses_the_exit_code() -> None:
    ok = Outcome(Item("a", "EGAF1"), True, bytes_moved=10, seconds=1.0)
    bad = Outcome(Item("b", "EGAF2"), False, "boom")
    reasons: Counter[str] = Counter({"ledger": 1})

    assert report([ok], reasons, 1.0, Canceller()) == 0
    assert report([ok, bad], reasons, 1.0, Canceller()) == 1

    stopped = Canceller()
    stopped.stop()
    assert report([ok, bad], reasons, 1.0, stopped) == 130


def test_failure_maps_exceptions_and_escalates_fatal_errno() -> None:
    item = Item("a", "EGAF1")
    cancel = Canceller()

    assert _failure(item, Cancelled(), cancel).detail == "interrupted"
    assert _failure(item, subprocess.TimeoutExpired("c", 1), cancel).detail == "timed out"
    assert _failure(item, FileFailure("nope"), cancel).detail == "nope"
    assert not cancel.stopped

    fatal = OSError(errno.ENOSPC, "No space left on device")
    outcome = _failure(item, fatal, cancel)
    assert outcome.ok is False
    assert cancel.stopped, "ENOSPC must stop the whole run"


def test_failure_names_an_exception_with_no_message() -> None:
    assert _failure(Item("a", "E1"), FileFailure(), Canceller()).detail == "FileFailure"
