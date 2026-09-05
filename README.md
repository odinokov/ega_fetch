# ega-fetch

[![Python](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Runtime dependencies](https://img.shields.io/badge/runtime%20deps-none-brightgreen)](pyproject.toml)
[![Typing](https://img.shields.io/badge/mypy-strict-blue)](pyproject.toml)
[![Lint](https://img.shields.io/badge/lint-ruff-purple)](pyproject.toml)

Download an EGA dataset from **Live Outbox** (SFTP + Crypt4GH) into a local
directory, decrypt it, and verify every file against the checksums the archive
publishes.

It is a *state-convergence* tool, not just a download script. You declare the target
state — "these files, present and verified, in this directory" — and run it as
many times as you like. It works out what is missing, fetches only that, and
resumes partial transfers byte-for-byte. Interrupting it is always safe.

```bash
ega-fetch --csv sample_file.csv --config ega.json --out /scratch/ega
```

---

## Install

```bash
pip install git+https://github.com/odinokov/ega_fetch.git
pip install crypt4gh          # external requirement, see below
```

The tool itself has **no runtime Python dependencies** — standard library only.
It shells out to two executables that must be on `PATH`.

| Requirement | Notes |
|---|---|
| Python **3.8+** | verified against CPython 3.8.20 and 3.10.16 |
| `sftp` | from OpenSSH; present on essentially every HPC node |
| `crypt4gh` | GA4GH reference implementation; decrypts the `.c4gh` objects |
| An ssh key **registered with EGA** | see below — this is the step with a lead time |

For development, `pip install -e ".[dev]"` adds pytest, mypy and ruff.

MIT licensed — see [LICENSE](LICENSE).

---

## Why not pyEGA3?

If your dataset accession starts with **`EGAD5`**, pyEGA3 cannot download it.
EGA's own documentation says so:

> "If the dataset you've been granted access to begins with the ID
> EGAD5XXXXXXX, please refer to the Live Outbox distribution."

pyEGA3 5.2.0 also still ships pointing at `ega.ebi.ac.uk:8443`, a legacy
endpoint whose token service hangs, and it reports every network failure as
`"Invalid username, password or secret key"`. This tool talks to Live Outbox
instead.

---

## Registering your key

> [!IMPORTANT]
> Registering the key with EGA has a lead time of **hours, not minutes**. Do
> this first — everything else is quick, and nothing will authenticate until
> the key has synced.

Live Outbox authenticates with an ssh key that is *also* the Crypt4GH key your
files are encrypted to. Generate an `ed25519` key, add the **public** half to
your EGA profile at <https://ega-archive.org>, and allow it to sync. Until it
has, the outbox returns:

```
Permission denied (publickey,keyboard-interactive).
```

That is the documented behaviour for an unregistered key, not a
misconfiguration. Verify with:

```bash
ssh -o BatchMode=yes -i ~/.ssh/id_ed25519 -l 'you@inst.edu' outbox.ega-archive.org
```

The private key must be `chmod 600`, or ssh refuses it. `ega-fetch` checks this
and tells you, but will not change the mode of your key -- that is yours to
manage.

---

## Input

A CSV with (at least) these two columns:

```csv
sample_accession_id,sample_alias,file_name,file_accession_id
EGAN50000086627,CGPLOV600P,CGPLOV600P.hg19.cram,EGAF50000163512
```

- `file_name` — the **decrypted** name. The outbox holds `<file_name>.c4gh`.
- `file_accession_id` — used to look up the published SHA-256 and file size.

Any other columns are ignored.

## Config file

Every setting can live in JSON, so the command line stays short. CLI arguments
always win over the file.

```json
{
  "username": "you@inst.edu",
  "private_key": "~/.ssh/id_ed25519",
  "dataset": "EGAD50000000695",
  "csv_path": "/home/you/sample_file.csv",
  "out_dir": "/scratch/you/ega",
  "jobs": 4,
  "io_timeout": 7200
}
```

Keep it `chmod 600`. It holds a *path* to your key, never key material.

---

## Python API

The console script and the importable API share one implementation, so they
cannot drift.

```python
from pathlib import Path

from ega_fetch import Canceller, Remote, Settings, run

settings = Settings(
    csv_path=Path("sample_file.csv"),
    out_dir=Path("/scratch/you/ega"),
    remote=Remote(
        host="outbox.ega-archive.org",
        port=22,
        username="you@inst.edu",
        key_path=Path.home() / ".ssh/id_ed25519",
    ),
    dataset="EGAD50000000695",
    c4gh_key=Path.home() / ".ssh/id_ed25519",
    jobs=4,
    io_timeout=7200,
    verify=True,
    recheck=False,
    refresh_manifest=False,
    keep_encrypted=False,
    dry_run=False,
)
exit_code = run(settings, Canceller())
```

`run` returns the same exit code the CLI would. `Canceller().stop()` from
another thread terminates in-flight transfers, leaving resumable partials.
The package ships a `py.typed` marker, so these hints reach your type checker.

---

## Running on SLURM

### The one-liner

```bash
srun -J ega -c 4 -t 48:00:00 --mem=4G --signal=TERM@300 \
  ega-fetch --config ega.json --out /scratch/$USER/ega -j 4 --log-file ega.log
```

That is the whole thing. A few notes on why each flag is there:

- **`--signal=TERM@300`** is the important one. SLURM sends `SIGTERM` 300 s
  before your walltime expires; `ega-fetch` catches it, terminates the in-flight
  `sftp` and `crypt4gh` children **immediately**, and exits `130` leaving
  resumable partials. Without it you get `SIGKILL` at the wall and lose the
  in-flight file's progress. Note there is no `B:` prefix — `B:` would signal
  only the batch shell, which never reaches the Python process. 300 s is far
  more than needed; shutdown takes under a second.
- **`-c 4` with `-j 4`** — match them. Each job is one `sftp` process plus one
  `crypt4gh` process, and both are largely I/O-bound.
- **`--mem=4G`** is generous. The tool streams; it holds one 4 MiB buffer per
  worker and never reads a file into memory.
- **`-t 48:00:00`** — size this from your measured throughput (see below).
  Under-booking is not a problem; see job chaining below.

### As a batch script

```bash
#!/bin/bash
#SBATCH --job-name=ega-fetch
#SBATCH --cpus-per-task=4
#SBATCH --mem=4G
#SBATCH --time=48:00:00
#SBATCH --signal=TERM@300
#SBATCH --output=ega-%j.log

module load python/3.11 || true          # site-specific
pip install --user --quiet crypt4gh      # once; skip if already installed

srun ega-fetch \
    --config "$SLURM_SUBMIT_DIR/ega.json" \
    --out "/scratch/$USER/ega" \
    -j "$SLURM_CPUS_PER_TASK" \
    --log-file "ega-$SLURM_JOB_ID.log"
```

### Chaining jobs when walltime is short

Because re-running is safe and cheap, a dataset larger than one walltime is just
a dependency chain. Each job converges what it can and the next picks up:

```bash
jid=$(sbatch --parsable ega.sbatch)
for i in $(seq 1 5); do
  jid=$(sbatch --parsable --dependency=afterany:$jid ega.sbatch)
done
```

`afterany`, not `afterok` — a job that exits `130` at the walltime signal did
useful work and the successor should still run.

### Do not use a job array

> [!WARNING]
> `ega-fetch` takes an exclusive `flock` on the output directory, because two
> concurrent runs would corrupt the ledger. Every array task after the first
> exits **4** immediately. Use one job with `-j N` for parallelism, never
> `--array`.

### Where to write

Point `--out` at fast scratch, not `$HOME`. Two reasons: NFS home directories
are usually slow and quota-limited, and `flock` semantics on some network
filesystems are unreliable.

---

## Sizing the job

Check what you are in for before booking 48 hours:

```bash
ega-fetch --config ega.json --out /scratch/$USER/ega --dry-run
```

```
manifest: 409 entries
initial state: 3 converged (3 ledger), 406 pending
plan: 406 file(s), 819.9 GB encrypted; 832.6 GB free in /scratch/you/ega
```

**Disk:** you need room for the encrypted bytes *plus* headroom to decrypt
`--jobs` files at once. The tool refuses to start otherwise:

```
preflight failed:
  - insufficient space in /scratch/you/ega: 832.6 GB free, 839.0 GB needed
    (encrypted, plus headroom to decrypt 4 file(s) at once)
```

Encrypted `.c4gh` files are deleted as each is decrypted, so the steady-state
footprint is roughly the decrypted total, but the peak needs that headroom.

> [!TIP]
> Measure one file before booking a long walltime — a CSV holding a single
> row is all it takes, and you get to choose *which* file.

**Time:** measure one file first rather than guessing.

```bash
head -1 sample_file.csv > one.csv          # header
grep CGPLOV621P sample_file.csv >> one.csv  # the row you want
ega-fetch --csv one.csv --config ega.json --out /scratch/$USER/ega
```

Observed on a university network at 2.5 MB/s per stream, a 1.7 GB file took
11m17s — about 4 days for 0.9 TB single-stream. Throughput is per-stream, so
raise `-j` and re-measure; prefer a node with good external bandwidth over a
laptop or VPN.

---

## Exit codes

| Code | Meaning | In a script |
|---|---|---|
| `0` | Converged — every requested file present and verified | done |
| `1` | Partial — some files failed | re-run; it retries only those |
| `2` | Usage or configuration error | fix the command; do not retry |
| `3` | Preflight failed, nothing transferred | fix the environment |
| `4` | Another run holds this output directory | retry later |
| `130` | Interrupted (SIGINT/SIGTERM) | re-run; partials resume |

`1` and `130` are worth retrying automatically. `2` and `3` never are.

---

## What it does per file

1. **Transfer** `<name>.c4gh` with `sftp reget` — resumes a partial byte-exactly.
2. **Check size** against the size the archive publishes.
3. **Decrypt** with `crypt4gh`, hashing the stream *as it is written*, so the
   bytes are read exactly once.
4. **Verify** the SHA-256 **before** publishing.
5. **Publish** with `os.replace` — atomic, so a crash never leaves a corrupt
   file where a good one belongs.
6. **Record** in `.ega_state.json` so the next run skips it with a single `stat`.

`--recheck` forces step 6 to be re-derived: every existing file is read and
hashed again instead of being trusted. You need it only for corruption that
*preserves the file size* — bit rot, a bad sector, an in-place edit. A
truncated or resized file is already refused on an ordinary run, because the
ledger records the size and a mismatch drops the entry. It is a no-op under
`--no-checksums`, and on a full dataset it means re-reading every published
byte, so it is not something to run routinely.

State lives in the output directory: `.ega_state.json` (ledger),
`.ega_manifest.json` (cached checksums and sizes), `.staging/` (in-flight),
`.ega_fetch.lock`. Deleting the ledger is safe — the next run re-verifies by
hashing, which is slower but reaches the same state.

---

## Common options

```
-j, --jobs N        concurrent file transfers (default 1)
-n, --dry-run       report the plan and exit
--recheck           re-hash every existing file instead of trusting the ledger
--refresh-manifest  refetch checksums instead of using the cache
--keep-encrypted    keep .c4gh files in staging after decryption
--no-checksums      transfer without verifying SHA-256 (not recommended)
--io-timeout SEC    per-file transfer timeout (default 14400)
--log-file PATH     append logs to a file as well as stderr
-v / -q             debug / warnings-only
```

`ega-fetch --help` lists the rest.

---

## Troubleshooting

**`Permission denied (publickey,keyboard-interactive)`** — the key is not
registered with EGA yet, or has not finished syncing. Hours, not minutes.

**`preflight failed: - could not fetch the manifest`** — the metadata API is
unreachable. `--no-checksums` will proceed without verification, but you lose
the size checks and the space budget too. Prefer waiting.

**Connection resets to `outbox.ega-archive.org:22`** — an institutional firewall
blocking outbound SSH. Test with
`ssh -o ConnectTimeout=15 outbox.ega-archive.org`; you should get an
`SSH-2.0-OpenSSH_9.x` banner and then `Permission denied`. Note that egress
policy can differ *between* networks at the same institution.

**`SHA-256 mismatch`** — the decrypted bytes do not match what the archive
published. The file is discarded rather than published; re-run to re-fetch. If
it recurs for the same file, contact <helpdesk@ega-archive.org>.

---

## Development

```bash
uv venv --python 3.8 .venv          # or any 3.8+
uv pip install --python .venv/bin/python -e ".[dev]"

.venv/bin/python -m pytest          # 81 tests
uv tool run ruff check src tests
uv tool run mypy                    # strict
```

The suite runs entirely offline, substituting exactly two seams: a fake `sftp`
(`tests/fake_sftp.py`), driven through the same argv and stdin protocol as the
real client, and a localhost stub for the metadata API. **`crypt4gh` is not
substituted** — the real binary encrypts each fixture and decrypts it again, so
the cryptographic path is exercised for real.

`tests/test_regression.py` is the important one: it runs the original
single-file `ega_fetch.py` and this package side by side over twelve scenarios
and asserts the exit code, log output, published bytes, ledger and manifest
cache are identical. Point it at the original with
`EGA_FETCH_ORIGINAL=/path/to/ega_fetch.py`; it skips if the file is absent.
