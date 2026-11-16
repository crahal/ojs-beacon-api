# API operations runbook

This runbook assumes [deployment](../HOW_TO_DEPLOY.md) is complete. Commands
run on the VM unless labelled otherwise. Substitute actual release dates and
your public origin; examples are not production addresses. Preserve local
configuration and data. Do not apply maintenance while a transfer, build,
credential adoption or publication is advancing.

## Routine checks

```bash
sudo systemctl show ojs-api-update.service -p ActiveState -p SubState -p Result -p ExecMainStatus
sudo journalctl -u ojs-api-update.service -n 50 --no-pager -o short-iso
sudo -u ojs -H bash -lc 'cd /srv/ojs_api && docker compose ps && docker compose logs --tail=50 mysql ojs-api'
curl --fail --show-error http://127.0.0.1:8000/health
df -h / /srv/ojs_api/data
free -h
sudo -u ojs docker stats --no-stream
```

An imported release uses `ojs-api-publish-YYYY-MM-DD.service`, while desktop
transfer uses the desktop's user unit. Check the unit for the **actual active
workflow**, not a failed updater from an earlier run. `inactive (dead)` is normal
after a successful oneshot; inspect `Result` and `ExecMainStatus`.

Also test trusted public HTTPS and authenticated `/meta`/`/snapshots` using
[the client guide](../HOW_TO_CALL.md) or `scripts/check_api_history.py`. Check
expected snapshot date, event high-water mark, anonymous rejection and backup
age, not just a healthy container. An old but healthy catalogue can be stale.

Monitor externally for public availability, certificate expiry/renewal failure,
unexpected 5xx responses and backup age. On-host email cannot report a powered-off
host. Review updates and accounts regularly; choose thresholds from measured
normal operation. Do not infer a guaranteed ETA from one elapsed stage.

## Logs and progress

```bash
sudo journalctl -fu ojs-api-update.service -o short-iso
```

Ctrl-C leaves this view; it does not stop the service. `[progress]` JSON contains
stage, event, UTC timestamp, elapsed/idle seconds and available byte/row counters.
A heartbeat proves the reporter is alive, not that SQL is advancing. Long SQL
statements may provide no row counter until completion. Compare CPU/I/O over
time as additional evidence:

```bash
systemctl show ojs-api-update.service -p MainPID -p CPUUsageNSec -p IOReadBytes -p IOWriteBytes -p MemoryCurrent -p MemoryPeak
sudo journalctl -k --since today --no-pager
```

Some kernels do not expose every accounting metric. Docker statistics cover
the serving containers; the builder runs on the host. Check systemd result and
OOM/kernel logs after crashes even when no final `failed` event was emitted.
See [progress semantics](../PROCESSING_GUIDE.md#reading-build-progress).

Docker logs rotate at 5 MiB per file with two files per service. The optional
journal policy bounds the whole host's journal; it is not a permanent audit
archive and size pressure can shorten retention. Keep small checksummed release,
change and cleanup reports with backups. Do not email raw data or secret-bearing
configuration as diagnostics.

Routine API access logs are disabled by default (`OJS_API_ACCESS_LOG=0`). Opting
in can record client IPs and query terms. Review access-log retention and access
permissions, including any separately enabled proxy logs, before enabling it.

## Daily scheduling

The cron entry starts `ojs-api-update.service` daily at **03:17 UTC**. It is a
full check/download/build/publish workflow, **not** a download-only scheduler.
Enable it only after the next full build's resource budget and backups have
been approved. Import-only serving can remain paused indefinitely.

```bash
cd /srv/ojs_api
sudo timedatectl set-timezone UTC
sudo systemctl disable --now ojs-api-update.timer
sudo install -o root -g root -m 644 deploy/ojs-api.cron /etc/cron.d/ojs-api
sudo systemctl enable --now cron
timedatectl show --property=Timezone --value
sudo cat /etc/cron.d/ojs-api
```

If the optional timer was never installed, disabling it reports "not found";
verify that no other scheduler exists before continuing. Do not install both
cron and the optional timer, and never replace an existing root crontab wholesale.
The checked-in per-user crontab example omits the system crontab's `root` field.

When ready to permit updates, remove only a verified regular pause marker:

```bash
sudo -u ojs -H bash -lc 'set -eu; cd /srv/ojs_api; test -f data/.updates-paused; test ! -L data/.updates-paused; rm -- data/.updates-paused'
```

The next cron run checks upstream validators. Unchanged data is a cheap no-op;
changed data is downloaded, checked and processed. The SQL footer determines
the snapshot date, not HTTP Last-Modified. Same-date content revisions stop for
review rather than overwriting history.

All repository source-changing entrypoints share `data/raw/.source-activity.lock`
through download, processing and publication. Another managed downloader/build
using that raw directory cannot overlap; `source_activity_busy` is a skipped
attempt, not queued work. An already-active systemd job is not restarted by
cron. An incomplete finalization blocks a new download for operator review.
Manual `curl`, `rsync` or filesystem writes do **not** participate: never add
or replace source files behind an active build. Never delete a lock file to
"unlock" a process.

Read-only source inspection remains available while paused or processing:

```bash
sudo -u ojs -H bash -lc 'cd /srv/ojs_api && make check-source'
```

## Pause and controlled shutdown

To block future automatic download/build/publication without stopping the live API:

```bash
sudo -u ojs touch /srv/ojs_api/data/.updates-paused
```

The marker is not cancellation. If stopping an advancing build is necessary,
first review its checkpoint/restart cost, then request graceful shutdown:

```bash
sudo systemctl stop --no-block ojs-api-update.service
sudo journalctl -fu ojs-api-update.service
```

Wait until the unit is inactive/failed and its MySQL process has exited before
touching data, changing code or rebooting. `deactivating` can mean InnoDB is
rolling back for hours. Keep the source lock and avoid SIGKILL. The service
deliberately has no forced-stop deadline. A reboot is not a clean shutdown.

The 720-hour coordinator limit is captured at startup; editing `.env` or
`daemon-reload` does not extend it mid-run. Avoid restarting valuable finalization
solely to refresh configuration.

## Diagnose failures before retrying

| Symptom | Safe next action |
| --- | --- |
| Beacon 401 | Correct the private source INI; rerun only `make check-source` |
| Disk reserve reached | Preserve failed candidate and logs; measure actual disk use and plan capacity, do not delete MySQL files |
| Memory limit/OOM | Inspect systemd/kernel evidence and peak; keep old release live and review working set |
| Heartbeats with no row counter | Compare current stage, CPU/I/O and available memory before calling it stalled |
| Release guard quarantined output | Review source/report and removal/merge counts; do not automatically override thresholds |
| Trusted HTTPS 503 | Check backend containers, publication result and MySQL readiness; TLS alone is not a catalogue |
| TLS failure | Check Caddy logs, static address/DNS, firewall and renewal; do not bypass certificate verification |
| Incomplete `.building` candidate | Read checkpoint/provenance and use only the applicable recovery path |

Downloads resume only when the saved upstream identity still matches. Validated
gzip/footer/checksum checks precede publication. Never edit a checksum, release
marker or checkpoint to suppress a mismatch.

Normal pipeline resume is limited to eligible checkpoints (not arbitrary
`finalizing` states); it can repeat metadata work. Exact-key/touched-key recovery
helpers are narrow audited bootstrap tools, not general restart commands. Read
[the recovery guide](../WORKSTATION_RECOVERY.md) before using them. Never force
rebuild or delete a failed candidate without an explicit recovery decision.

## Publication, retry and rollback

`mysql-current` is the completed build candidate; `mysql-live` is the serving
release. `.compact-update.json` is the coordinator's durable switch journal.
It is not an expendable cache. Before promotion the coordinator cold-validates
the candidate, retains the old directory, switches/recreates containers, then
performs authenticated health/provenance checks. A failed switch restores the
previous release; interrupted switching is recovered through the journal.

Cold validation checks the catalogue counts and source-to-identity consistency
across the full dataset. On a small VM it can take hours even when transfer is
complete. The later HTTP health-check timeout is not a time limit for this SQL
validation. Follow the validation stage and measured CPU/I/O; do not bypass it
or launch another publisher because the API is not serving yet.

For a completed **credential-compatible** candidate, explicitly publish its
date without downloading, rebuilding or pruning. This works while paused:

```bash
sudo -u ojs -H bash -lc 'cd /srv/ojs_api && ./scripts/automatic_update.sh --publish-release YYYY-MM-DD --no-prune'
```

For a long publication, run under a reviewed systemd service instead of relying
on a terminal session. An imported candidate with different credentials must
use `publish_imported_release.py`/the handoff workflow first. A successful
command exit is insufficient if it reported a lock-related skip; verify the
selected live release and authenticated API.

`make publish-live` selects `mysql-current`, respects the pause marker and may
perform normal cleanup; it is not the preferred paused/manual recovery command.
Do not repoint symlinks, erase the journal, or start another MySQL process on a
mounted serving directory.

Automatic rollback protects a **failed publication**, not arbitrary future
operator mistakes. After a successful newer release, the coordinator explicitly
refuses to publish an older date. A deliberate restore to older state requires a
separate maintenance/restore plan and client cursor reconciliation, not that
command with an older date. Cleanup may already have removed prior directories.

## Retention and backups

Normal automatic publication keeps the latest validated raw gzip, current
physical database, current compressed clean export and small audit reports.
During building and verification the prior live directory is retained. Only
after successful live checks can older recognised managed artifacts be removed.
Cleanup records filenames/identities/bytes in `pkpbeacon-cleanup-DATE.json`.
Unknown files, incomplete builds, mounted databases and unsafe symlinks are
protected. The imported-release route requests no pruning.

These retained large artifacts are not interchangeable duplicates: raw data
supports replay, the datadir serves queries, and the clean export carries
cumulative state into a future build. Do not remove the current export just
because the API works. Do not install dated bootstrap cleanup examples without
reviewing and authorising their exact targets. Cleanup is permanent.

Keep at least one recoverable **off-host** copy of the latest clean export,
its checksum, release manifest and all small change reports/checksums. Retain
authorised historical raw inputs separately if exact replay is required. Back up
private configuration/credentials encrypted with restricted access, separately
from any public repository. Record code revision, MySQL patch and restore steps.

A logical export is produced from the validated candidate and published as a
completed artifact only after clean database shutdown. Validate its SHA-256 and
copy the immutable file rather than making another live SQL dump. For a physical
datadir or whole-volume snapshot, stop the builder and
serving database cleanly first; a live file copy is not a consistent backup.
Budget provider snapshot/storage/transfer charges and apply bounded retention.

Test restoration to an **isolated** host/filesystem with the matching MySQL
patch, correct private credentials and the reports required by validation. A
logical SQL export alone is not a drop-in `mysql-live` directory: restore it to
a new database and validate/package it through a reviewed recovery procedure.
Do not claim disaster recovery from an untested archive. A cold physical backup
can use the completed-release handoff when its full package is present.

## Application and dependency upgrades

1. Pause automatic work; wait for active transfer/build/publication and its
   native builder to finish. Leave the healthy serving MySQL running for an
   API-only rollout. Do not change files underneath an advancing worker.
2. Record the current Git revision, image IDs/digests, live snapshot, effective
   service drop-ins and backup verification. Keep private settings unchanged.
3. Fetch/review a specific release or commit in a clean checkout. Inspect
   migrations and run the documented tests on representative fixtures first.
   Do not use `git reset --hard` to erase local operator changes.
4. Build an explicitly tagged API image from that separate reviewed checkout.
   Keep the deployment's private source/configuration intact; the image helper
   neither installs source files nor changes the operational Git remote.
5. Use the guarded image-only rollout below. Review service/proxy changes
   separately; MySQL version/schema or credential changes need their own
   maintenance plan. Check authenticated history and external HTTPS afterwards.
6. Resume the scheduler only after health, capacity and backups are confirmed.

### Roll out a prebuilt API image

This workflow is for an **image-only** update after initial publication has
finished successfully. It requires healthy existing MySQL/API containers,
trusted public HTTPS, unchanged working credentials/configuration, a regular
`data/.updates-paused` marker, existing workflow locks and no pending
publication journal. It is not a first-deployment, source-upgrade, credential
rotation or database-recovery command.

Prepare a separate clean canonical checkout at `/srv/ojs-api-review`, owned
by `ojs`, following [the contribution workflow](../CONTRIBUTING.md). Check out
and test an explicit reviewed revision there. Do not copy private files into
the image build context or overwrite the operational checkout. On the **VM**,
build the local image from that reviewed source:

```bash
sudo -u ojs -H bash -lc 'set -eu; cd /srv/ojs-api-review; git diff --quiet; git diff --cached --quiet; test -z "$(git ls-files --others --exclude-standard)"; git rev-parse HEAD; docker build --tag ojs-api:reviewed .'
```

Confirm the displayed source revision is the one approved. Image building can
consume resources; do it only with adequate headroom and no advancing
publication/build. The following command uses the **already local** image and
asks for all expected published snapshot dates, oldest first:

```bash
read -r -a OJS_EXPECTED_SNAPSHOTS -p 'All published snapshot dates, oldest first, separated by spaces: '
OJS_IMAGE_CHECK_ARGS=()
for OJS_DATE in "${OJS_EXPECTED_SNAPSHOTS[@]}"; do
    OJS_IMAGE_CHECK_ARGS+=(--snapshot "$OJS_DATE")
done
if test "${#OJS_EXPECTED_SNAPSHOTS[@]}" -eq 0; then
    printf 'No dates supplied; rollout not started.\n' >&2
else
    sudo -u ojs python3 -B /srv/ojs-api-review/scripts/update_api_image.py \
      --project-root /srv/ojs_api --prebuilt-image ojs-api:reviewed \
      --health-timeout 300 "${OJS_IMAGE_CHECK_ARGS[@]}"
fi
```

The helper acquires the workflow locks, verifies baseline loopback/public
authentication and history, retains a rollback tag for the old image, and
recreates **only** the API with no build, pull or dependency restart. It verifies
the selected image, unchanged MySQL identity/start time and unchanged catalogue
metadata afterwards. A failed rollout attempts to restore and verify the old
image; a failed rollback requires operator investigation. The 300-second value
bounds individual health/wait operations, not the entire multi-step procedure.
Expect a brief API interruption and keep the operator session connected until
the command reports its result.

Automatic updates stay paused. The old image's rollback tag is retained and
references existing layers rather than making another copy of them. Keep it
until acceptance, then review image retention separately; the helper performs
no pruning. If the selected image is already running, it verifies health without
recreating the container. This does not apply edited credentials or configuration.

The helper never installs source files, rebuilds data, restarts MySQL or resumes
scheduling. Apply any separately reviewed source/service updates through the
private deployment workflow after active work is finished. Never downgrade
MySQL over a datadir upgraded by a newer server; use a compatible tested backup.

## Credential rotation

The application has one shared HTTP Basic identity, not per-user accounts,
self-service signup or a multi-key grace period. Rotate in a planned window:

1. Pause updates and wait for publication/build activity to finish.
2. Generate a new URL-safe key in a password manager. Edit only
   `.secrets/api-client.env` privately as `ojs`; preserve its two expected keys,
   ownership and mode 600. Do not rerun the initial credential generator.
3. In the separate credential-maintenance procedure, recreate only `ojs-api`
   using the unchanged accepted image, without building/pulling it or restarting
   MySQL. An editor's new inode also needs the bind mount refreshed. Keep an
   approved private rollback copy of the old credential until verification.
4. Verify the new identity succeeds and old credentials fail over trusted HTTPS;
   distribute the replacement securely, then resume approved operations.

Do not use `update_api_image.py` for this rotation: its baseline check requires
the existing credentials to work, and selecting the same image deliberately
does not recreate the container. Rotation needs a separately reviewed procedure
that protects its credential update and recreation from competing workflows.

Do not confuse that key with Beacon authentication or the API's MySQL password.
Changing `MYSQL_ROOT_PASSWORD` or `.secrets/db-api-password` alone does not update
existing database accounts. Those rotations require a backed-up maintenance
plan that updates accounts and all relevant completed candidates/exports,
then proves credentials and SELECT-only grants. The incoming-candidate adoption
helper intentionally refuses to rekey the live database.

Keep source passwords, API keys and SQL administrative credentials out of
command arguments, shell history, diagnostics and issues. Revoke exposed keys
at their issuer; deleting them from the latest Git tree is not revocation.

## Optional progress email

Email reporting is independent of serving and not required for deployment. The
default reporter observes `ojs-api-update.service`; it does not automatically
follow a desktop transfer or a differently named publication unit. Confirm its
target before treating an email as current deployment status. It is not an
external availability monitor and a summary is not proof of successful release.

Preview locally without sending mail or loading credentials:

```bash
sudo runuser -u ojs -g systemd-journal -- python3 -B /srv/ojs_api/src/email_progress.py --dry-run
```

For an approved Gmail sender, create a dedicated app password privately and use
the helper's hidden prompt; never paste it into chat or command arguments.
Substitute authorised sender/recipient addresses for `YOUR_GMAIL`/`YOUR_EMAIL`.
The chained commands enable scheduling only if configuration and a test send
succeed:

```bash
cd /srv/ojs_api
sudo install -o root -g root -m 644 deploy/ojs-api-progress-email.service /etc/systemd/system/ojs-api-progress-email.service
sudo install -o root -g root -m 644 deploy/ojs-api-progress-email.timer /etc/systemd/system/ojs-api-progress-email.timer
sudo systemctl daemon-reload
sudo python3 /srv/ojs_api/scripts/configure_progress_email.py \
    --sender YOUR_GMAIL --recipient YOUR_EMAIL &&
sudo systemctl start ojs-api-progress-email.service &&
sudo systemctl enable --now ojs-api-progress-email.timer
```

Check actual inbox/spam delivery as well as SMTP acceptance, then inspect the
schedule. If the test did not arrive, disable the timer while investigating:

```bash
sudo systemctl list-timers ojs-api-progress-email.timer --all --no-pager
```

The timer sends every 12 hours, restarting its interval after reboot; missed
messages are not replayed. The configuration helper refuses to overwrite
existing private files. Additional-recipient templates and optional AI summary
support are opt-in. For an approved optional AI summary, the following chain
privately installs the API credential, performs one paid preview, and installs
the drop-in only after that preview passes:

```bash
cd /srv/ojs_api
sudo python3 /srv/ojs_api/scripts/configure_progress_openai.py &&
sudo bash /srv/ojs_api/scripts/preview_progress_ai.sh &&
sudo install -d -o root -g root -m 755 /etc/systemd/system/ojs-api-progress-email.service.d &&
sudo install -o root -g root -m 644 deploy/ojs-api-progress-email-openai.conf /etc/systemd/system/ojs-api-progress-email.service.d/30-openai.conf &&
sudo systemctl daemon-reload
```

The key helper refuses to overwrite an existing credential. If preview fails
after key creation, correct the cause and resume at the preview line, not at
key creation. Do not enable the drop-in until preview succeeds. This starts no
extra email or data-processing run. The reporter sends selected sanitised
observations, not raw logs/data, for an optional interpretation. It falls back
to measured reports if unavailable and cannot control the server. Its local
request-count allowance is not an account-wide spending cap; review code and
current provider pricing before enabling. API usage costs are separate from
hosting costs, and the feature is off by default.

Disable future messages with
`sudo systemctl disable --now ojs-api-progress-email.timer`; this does not stop
the API or update job. Revoke reporting credentials when no longer needed.
