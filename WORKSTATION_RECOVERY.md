# Build and recover history on a workstation

Use a separate Linux workstation when the serving VM cannot safely accommodate
the build's memory or storage peak. The server can serve the last completed
release while the workstation prepares the next. Start from a reviewed clone;
data and temporary build credentials are supplied separately, never via Git.

## Prepare an isolated workspace

Keep mutable MySQL files, credentials and scratch space outside cloud-sync and
automatic backup roots. Git ignore rules do not stop Dropbox or other sync
clients. Use a disk-backed scratch filesystem, not tmpfs. An example layout is:

```text
/srv/ojs-workstation/
  repo/           # reviewed source checkout
  raw/            # authorized immutable compressed dumps
  clean/          # completed releases and candidate checkpoints
  scratch/        # disk-backed MySQL temporary files
  credentials/    # private mode-0700 directory
```

After provisioning `/srv/ojs-workstation` for the build user, clone the canonical
source into the new, absent `repo` directory and run the following examples
from that checkout:

```bash
git clone https://github.com/crahal/ojs-beacon-api.git /srv/ojs-workstation/repo
cd /srv/ojs-workstation/repo
```

Do not clone over an existing workspace or import a private operational Git
history. Preserve the running builder's checkout until its work is complete.

The scratch path must not contain a component named `data`, `raw` or `clean`.
Install the same exact MySQL 8.4 patch as the server, including `mysqld`, `mysql`,
`mysqladmin` and `mysqldump`. Check each executable's `--version`.
The wrapper requires all four resolved binaries in one directory; examples use
an explicitly installed, verified distribution under `/opt/mysql/bin`. Ubuntu's
split `/usr/bin` and `/usr/sbin` layout does not satisfy that wrapper contract.

Generate temporary **builder-only** credentials as the build user:

```bash
python3 scripts/create_local_build_credentials.py --directory /srv/ojs-workstation/credentials
```

This refuses overwriting existing credentials. Do not copy production secrets
to the workstation. Review [configuration](docs/CONFIGURATION.md) before a
fresh baseline build with `src/run_pipeline.py`; `workstation_recover.py` is
specifically for an eligible stopped bootstrap checkpoint, not a universal
resume command.

## Build a fresh baseline

For an authorized baseline dump already under `raw`, create empty owned `clean`
and disk-backed `scratch` directories and a reviewed source clone at `repo`.
The following example deliberately keeps temporary database secrets in a private
systemd environment file, not command arguments. It assumes sufficient available
RAM for an 8 GiB cgroup **plus** the 8 GiB host reserve; adjust only after capacity
review. Do not run this against a serving or transferred datadir.

```bash
systemd-run --user --unit=ojs-baseline-build \
  --property=Type=exec \
  --property=WorkingDirectory=/srv/ojs-workstation/repo \
  --property=EnvironmentFile=/srv/ojs-workstation/credentials/local.env \
  --property=MemoryHigh=6G --property=MemoryMax=8G \
  --property=MemorySwapMax=1G --property=CPUQuota=400% \
  --property=KillMode=mixed --property=TimeoutStopSec=infinity \
  --property=SendSIGKILL=no --property=UMask=0077 \
  --setenv=PATH=/opt/mysql/bin:/usr/bin:/bin \
  --setenv=OJS_DB_API_PASSWORD_FILE=/srv/ojs-workstation/credentials/db-api-password \
  --setenv=OJS_MYSQL_TMP_ROOT=/srv/ojs-workstation/scratch \
  /usr/bin/python3 scripts/run_guarded_local_build.py \
  --working-directory /srv/ojs-workstation/repo \
  --disk-reserve /srv/ojs-workstation/clean 40 \
  --disk-reserve /srv/ojs-workstation/scratch 40 \
  --min-available-memory-gib 8 --min-cgroup-headroom-gib 2 \
  --require-cgroup-limit --max-runtime-hours 720 \
  -- /usr/bin/python3 src/run_pipeline.py \
  --snapshot /srv/ojs-workstation/raw/pkpbeacon-2026-01-01.sql.gz \
  --raw-dir /srv/ojs-workstation/raw --clean-dir /srv/ojs-workstation/clean \
  --skip-download --compact-storage --mysql-buffer-pool-size 2G --metadata-workers 1
```

This starts a long-running build, not a dry run. Keep the workstation powered on
and awake, and ensure its user manager survives any planned logout. Follow
`journalctl --user -fu ojs-baseline-build.service`. A subsequent history job must
wait for successful validation/export and clean MySQL shutdown.

## Continue a completed baseline chronologically

The baseline must have a valid clean export, checksum and release report under
`clean`. Supply each later raw snapshot in date order. Example dates below are
illustrative, not downloaded or included in the repository:

```bash
python3 scripts/workstation_history.py \
  --credentials-env /srv/ojs-workstation/credentials/local.env \
  --api-password-file /srv/ojs-workstation/credentials/db-api-password \
  --mysql-bin /opt/mysql/bin --mysql-version 8.4.11 \
  --baseline-version 2026-01-01 \
  --snapshot /srv/ojs-workstation/raw/pkpbeacon-2026-07-01.sql.gz \
  --snapshot /srv/ojs-workstation/raw/pkpbeacon-2026-10-01.sql.gz \
  --clean-dir /srv/ojs-workstation/clean \
  --scratch-dir /srv/ojs-workstation/scratch \
  --buffer-pool-size 4G --metadata-workers 1 \
  --min-available-memory-gib 8 --disk-reserve-gib 40 --check-only
```

This initial check does not scan all large inputs or start MySQL. Remove
`--check-only` only after reviewing the plan, and run through
`scripts/run_guarded_local_build.py` inside a bounded systemd service. The guard
accepts repeated `--disk-reserve PATH GIB`, host/cgroup memory reserves and a
runtime limit; consult `--help` for the complete argument contract. Do not
equate installed RAM with currently available memory or omit scratch reserves.

Set explicit `MemoryHigh`, `MemoryMax`, `MemorySwapMax` and `CPUQuota` suited to
the workstation; use `--require-cgroup-limit`. Keep `KillMode=mixed`,
`TimeoutStopSec=infinity` and `SendSIGKILL=no` so interruption allows MySQL
rollback. Keep the machine awake and powered on. User services need a persistent
user manager to survive logout; closing a terminal is not the same as rebooting.

No pruning is requested in this example. Optional pruning requires a reviewed
backup/retention decision. `--native-only-cleanup` is an assertion that selected
databases were never container-managed, not a shortcut around Docker checks.

## Recovery boundaries

Inspect `OJS_PIPELINE_STATE.json`, the matching source checksum, build SQL
fingerprint and current processes before resuming anything. Normal resume is
limited to eligible unchanged metadata checkpoints. Bootstrap exact-key recovery
has separate guards; arbitrary interrupted finalization is not automatically
resumable. Preserve a stopped failed candidate until its phase and recoverability
are understood. Do not edit checkpoint markers to force acceptance.

The metadata storage profile supports long metadata fields and deferred payload
materialization. A stale profile/checksum is a compatibility issue to inspect,
not justification to delete or truncate data. Resource monitors intentionally
stop useful work when reserves are reached; rollback can continue beyond the
work deadline. Never force-kill a rolling-back database to make it look finished.

## Completion and handoff

Require successful release guards, validated checksums/reports, all expected
snapshot rows, and a cleanly stopped MySQL process. A heartbeat or a `.building`
directory does not establish completion. Use the [release handoff guide](docs/API_HISTORY_HANDOFF.md)
to transfer the latest database/export and cumulative history. Keep the source
copy until authenticated public API verification succeeds. Transfer charges and
backup costs remain the operator's responsibility.
