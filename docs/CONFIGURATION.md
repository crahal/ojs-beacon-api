# Configuration reference

Start with [deployment](../HOW_TO_DEPLOY.md). The authoritative defaults are
[`.env.example`](../.env.example), [Compose](../docker-compose.yml) and the
[systemd update unit](../deploy/ojs-api-update.service). This reference explains
their operational meaning; changing a value does not resize an AWS resource.

## Private files

| File | Purpose | Handling |
| --- | --- | --- |
| `.env` | Host/publisher settings, including MySQL root password | `ojs` owned, mode 600; never commit or print |
| `.secrets/api-client.env` | Original internal HTTP Basic username/key | Mode 600; preserve for monitoring/publication |
| `.secrets/api-clients/clients.json` | Optional independently revocable client key hashes | Mode 600, parent mode 700; never put plaintext keys or invitations here |
| `.secrets/db-api-password` | SELECT-only MySQL account used by the API | Mode 600; never give to API clients |
| `.secrets/beacon.ini` | Upstream source authentication | Mode 600; obtain through an authorised channel |
| `.secrets/incoming-root-password-DATE` | Temporary credential for an imported stopped database | Created privately by the transfer helper; retained for reviewed recovery |

Make `.secrets` mode 700. Run the credential generator only on first setup; it
refuses to overwrite secrets. Importing a database does not replace the VM's
client credentials. The adoption helper changes only the stopped candidate to
use the destination's root/read-only credentials.

`.env` is consumed by Bash, Docker Compose and systemd. Use plain `NAME=value`
assignments and URL-safe generated secrets; avoid `export`, shell expressions,
spaces and interpolation characters. This file is trusted executable input to
the shell wrapper. Do not source unreviewed configuration from another person.
Source passwords containing punctuation belong in the INI file, not `.env`.

## Identity and serving

| Setting | Meaning |
| --- | --- |
| `MYSQL_ROOT_PASSWORD` | Administrative password for builder/publication; changing the file alone does not change an existing database account |
| `OJS_ADMIN_EMAIL` | Contact address used in API/OAI metadata |
| `OJS_PUBLIC_BASE_URL` | Actual trusted HTTPS origin, no path; use your stable IP or DNS hostname |
| `OJS_API_CREDENTIALS_HOST_FILE` | Host credential file mounted read-only into the API; default `./.secrets/api-client.env` |
| `OJS_DB_API_USER` | SELECT-only database user, default `ojs_api`; not root |
| `OJS_DB_API_PASSWORD_FILE` | Private password file for that database user |
| `OJS_DB_NAME` | Catalogue schema, default `pkpbeacon_db`; keep consistent across build/serve |
| `OJS_MYSQL_IMAGE` | Exact numeric MySQL patch tag, matching host and physical candidate |
| `OJS_PYTHON_IMAGE` | Pinned Python base image used when building the API image |
| `OJS_HOST_UID`, `OJS_HOST_GID` | Numeric IDs of the `ojs` account that owns the data |
| `OJS_API_PORT` | Host loopback API port, default 8000; update the proxy and checks if changed |
| `OJS_OAI_PAGE_SIZE` | OAI page bound, default 50 |
| `OJS_API_MAX_PAGE_SIZE` | JSON page bound, default 200 |
| `OJS_API_CONCURRENCY` | API concurrency bound, default 8; not a per-client quota |
| `OJS_API_CLIENTS_FILE` | Optional native hash registry path inside the API container; blank disables native additional accounts, not any separately configured Caddy gateway |
| `OJS_API_CLIENTS_HOST_DIR` | Dedicated host directory mounted read-only at `/run/secrets/ojs-api-clients`; default `./.secrets/api-clients` |
| `OJS_API_ACCESS_LOG` | Default `0`: routine request access logs are disabled. Opting in can record client IPs and query terms; keep those logs private with deliberate retention |

The coordinator sets `OJS_LIVE_DATA_DIR` when selecting the physical database.
Do not hardcode a stale directory in `.env` or manually repoint `mysql-live`.
Production API credentials are loaded at process startup; see
[rotation](OPERATIONS.md#credential-rotation) before editing them.

## Source, processing and safeguards

| Setting | Default | Meaning |
| --- | --- | --- |
| `OJS_BEACON_URL` | Authenticated Beacon gzip URL in the template | Upstream full snapshot; no historical archive discovery is implied |
| `OJS_BEACON_CREDENTIALS_FILE` | `.secrets/beacon.ini` | Private `[beacon]` username/password INI |
| `OJS_COMPACT_STORAGE` | `1` | Native compression and bounded intermediate storage |
| `OJS_MYSQL_COMPRESSED_PAGE_KB` | `8` | Compressed InnoDB page size for a new compact build: exactly `8` or `4`. The opt-in 4 KiB layout also rewrites predecessor export storage declarations, without changing SQL values. Requires a measured capacity review; 2 KiB is not supported |
| `OJS_DEFER_BASELINE_PAYLOAD` | `0` | With compact storage, import the predecessor without its large article payload fields; restore retained payload from that same checksum-verified export after temporary work tables are reclaimed. Requires a complete predecessor; never enable it on an incompatible interrupted build |
| `OJS_COMPRESS_EVENT_HISTORY` | `0` | Compress the candidate's event-history table using the selected 8 or 4 KiB layout. Original event values, IDs and ordering remain unchanged; checkpoints and physical-layout audits bind this option |
| `OJS_RETIRE_BASELINE_EXPORT` | `0` | Opt-in early removal of the redundant predecessor gzip, only after the successor has been fully hydrated, validated and saved at a durable export-ready checkpoint. Requires compact storage and baseline payload deferral. The predecessor's live database remains available for rollback until successor publication passes its checks |
| `OJS_BUILD_EXPORT_GB` | Unset | Positive export working-space budget required by the retirement workflow. Actual available space must cover this budget plus the data reserve after retirement and before export; it is not inferred from compressed input size |
| `OJS_BASELINE_EXPORT_BACKUP_RECEIPT` | Unset | Optional absolute path to a private, operator-owned receipt for a separately verified predecessor export backup. If configured, it must match the predecessor's date, checksum and size. A stale receipt blocks the next build; leave unset for the normal two-database handoff policy |
| `OJS_METADATA_WORKERS` | `1` | XML metadata workers; leave at one on a 4 GiB host |
| `OJS_PROGRESS_SECONDS` | `30` | Heartbeat interval; finite 1–3600 seconds |
| `OJS_BUILD_WORKSPACE_GB` | Unset (builds blocked) | Reviewed **additional** peak working space in GiB for a new candidate, staging/indexes and final export; include growth headroom. Existing data already consumes measured free space. Required before unattended builds, not publication of completed releases |
| `OJS_BUILD_SCRATCH_GB` | Unset | Additional scratch budget in GiB, required whenever `OJS_MYSQL_TMP_ROOT` is configured. Omit that scratch component from the workspace budget, even when both paths share a filesystem |
| `OJS_MIN_FREE_GB` | `20` | Minimum starting free space, measured in GiB; not estimated build peak |
| `OJS_DISK_RESERVE_GB` | `8` | Stop guard while processing, measured in GiB |
| `OJS_SCRATCH_RESERVE_GB` | Unset (inherits data reserve) | Optional stop guard for a separate scratch filesystem. If scratch shares a filesystem with data/raw input, the larger reserve applies; a smaller override cannot weaken the data guard |
| `OJS_MIN_AVAILABLE_MEMORY_MB` | `512` | Minimum currently available host memory, measured in MiB |
| `OJS_UPDATE_NICE` | `15` | Host CPU scheduling niceness; 0–19 |
| `OJS_MAX_RUNTIME_HOURS` | `720` | Coordinator work cutoff, captured at process startup |
| `OJS_MYSQL_TMP_ROOT` | Unset | Optional dedicated scratch filesystem; requires matching access/mount rules and its own reserve |
| `OJS_MAX_ACTIVE_SOURCE_DROP_FRACTION` | `0.10` | Quarantine unexpectedly large active-source loss |
| `OJS_MAX_ACTIVE_ARTICLE_DROP_FRACTION` | `0.05` | Quarantine unexpectedly large active-article loss |
| `OJS_MAX_ARTICLE_REMOVAL_FRACTION` | `0.05` | Quarantine unexpectedly large removals |
| `OJS_MAX_ARTICLE_MERGE_FRACTION` | `0.01` | Quarantine unexpectedly large merges |

The build admission check groups data, raw input and configured scratch by
filesystem. Each filesystem must accommodate the sum of its working budgets,
the advertised new download (if needed), and the reserve; available space is
never pooled between different disks. The minimum start floor is checked too.
Without a configured `OJS_MYSQL_TMP_ROOT`, include SQL temporary space in
`OJS_BUILD_WORKSPACE_GB` itself. Budget values must be finite and positive. They are operator-reviewed estimates,
not automatic predictions or a guarantee that the build will fit. Revisit them
as the catalogue grows. A large disk that holds the serving database may still
be too small for a full build beside it.

With early export retirement enabled, review the maximum **simultaneous**
working footprint across phases: raw import and staging; fully hydrated
candidate with the old export; and candidate with the new export after the old
export is removed. Do not simply subtract an archive from an otherwise unmeasured
budget. The old live database, source archive and retained history remain present.
The early-removal policy saves coexistence of two logical exports; it does not
remove history rows or relax matching, row-loss or publication checks.

The export-ready checkpoint embeds its validated report and source/build/storage
bindings. An interrupted export can resume without replaying SQL wrangling or
creating duplicate events. The coordinator handles that checkpoint before
checking for a newer upstream download. It checks the separate export budget,
then the runner revalidates the database and checks actual free space before
exporting. The event-ID allocator survives the cold verification restart.
A complete, authenticated export left by an interrupted publication can be
reused without storing a second export. A committed successor still awaiting
promotion follows the existing database and API validation checks before going
live; it does not trigger another download or rebuild.
Unknown or inconsistent checkpoints are retained for review rather
than deleted. If a candidate cannot be recovered after predecessor export
retirement, preserve the old live database and regenerate its logical export
from that database or restore a verified external backup before rebuilding.

Leave `OJS_MYSQL_TMP_ROOT` unset to keep SQL temporary files beside the candidate
on the data filesystem; include that temporary space in the workspace budget.
The operating-system root filesystem still hosts small runtime files such as
sockets and service logs. There is no requirement to retain a second data disk.

The optional 4 KiB layout affects the new candidate, not the existing serving
database. It trades additional compression work for potentially smaller tables;
it does **not** guarantee a particular compression ratio or that a build fits.
The source archive, original source checksum, article identities and temporal
matching rules are unchanged. Reports and checkpoints record the physical
layout separately. Changing the layout of an interrupted candidate is rejected
before cleanup, including with force rebuild; review that candidate rather than
deleting it or changing the setting to make a retry pass. Keep strict MySQL
checks enabled. The narrower 2 KiB layout cannot hold the catalogue schema and
is rejected before creating a database.

Baseline payload deferral changes **temporary storage**, not the catalogue's
contents. The predecessor export must remain available until hydration finishes.
Unchanged, removed and merged articles recover their original payload; articles
with newly selected canonical payload are not overwritten. Each restored payload
is compared byte-for-byte (including NULL distinctions) before its tracking ID is
cleared. Missing rows, changed input checksums or unfinished hydration block
validation/export/publication. The live API continues reading the existing
published database throughout. This option needs a second streaming read of the
predecessor and can take longer. Resume is deliberately restricted to the verified
`metadata_ready` boundary; other interrupted phases require operator review.

Measure final candidate **plus** new clean export, growth and hydration
fragmentation as well as the raw/staging peak. Smaller intermediate tables alone
do not establish that the whole release fits. Keep the original export and live
database until the normal verified publication/retention workflow permits cleanup.

Admission runs before downloading and again before building. An unchanged,
already-validated upstream release remains a cheap no-op without a configured
build budget. Explicit publication/recovery keeps its existing separate guards.
An interrupted candidate also occupies measured space: do not lower the budget
merely to make a retry pass; review the actual remaining work.

`make automatic-readiness` performs bounded metadata/lock/configuration checks
and an upstream HEAD request; it never hashes the large export, downloads data,
starts MySQL, sends mail, calls an LLM, enables scheduling or removes a pause.
`ready_for_build` excludes the intentional pause/scheduler state;
`automatic_updates_allowed` also requires those activation checks. Exit code 3
means build admission is blocked, while exit 0 may still mean `status=prepared`
and automatic execution remains disabled. A positive result does not verify
backups, public API health, duplicate entries in arbitrary user crontabs, or
the complete effective systemd configuration; operators must review those too.

The service's outer work timeout is 31 days. Graceful database rollback may
continue beyond either timeout; `TimeoutStopSec=infinity` intentionally avoids
force-killing InnoDB. Editing `.env` or reloading systemd does not extend a
deadline already captured by a running coordinator.

Release-guard failures require investigation, not a routine threshold increase.
`--allow-anomalous-release` is an explicit pipeline override for a reviewed
case; never bake it into cron. A missing/changed same-date source also needs
review rather than silent replacement.

## Memory limits

| Component | Main defaults |
| --- | --- |
| Host builder | `OJS_MYSQL_BUFFER_POOL_SIZE=768M`, max connections 8, TempTable RAM 64M |
| Builder systemd cgroup | `MemoryHigh=1792M`, `MemoryMax=2048M`, `MemorySwapMax=1024M` |
| Serving MySQL | Buffer pool 512M, max connections 16, TempTable RAM 64M |
| Serving MySQL container | Memory 1024m, reservation 512m, memory+swap 1536m |
| API container | Memory 256m, memory+swap 384m |

Docker's `memswap_limit` is a combined RAM-plus-swap bound; systemd's
`MemorySwapMax` is swap only. These are different controls. Buffer pools are
only part of MySQL's memory use. The remaining host budget must accommodate
Ubuntu, Docker, Caddy, filesystem cache and other agents. Swap does not make a
larger working set fast or guarantee success under pressure.
[Docker memory limits](https://docs.docker.com/engine/containers/resource_constraints/),
[systemd resource controls](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html).

The corresponding variable names are `OJS_BUILDER_MYSQL_MAX_CONNECTIONS`,
`OJS_BUILDER_MYSQL_TEMPTABLE_MAX_RAM`,
`OJS_SERVING_MYSQL_BUFFER_POOL_SIZE`, `OJS_SERVING_MYSQL_MAX_CONNECTIONS`,
`OJS_SERVING_MYSQL_TEMPTABLE_MAX_RAM`, `OJS_SERVING_MYSQL_MEMORY_LIMIT`,
`OJS_SERVING_MYSQL_MEMORY_RESERVATION`, `OJS_SERVING_MYSQL_MEMSWAP_LIMIT`,
`OJS_API_MEMORY_LIMIT` and `OJS_API_MEMSWAP_LIMIT`.

## Optional scraper and reporting

`OJS_SOURCE_INDEX_URL` is the optional standalone HTML-index scraper, not the
compact daily Beacon coordinator. Leave it empty unless deliberately using that
workflow. Its optional username/password, cross-origin policy and expanded-size
limit must be reviewed before use. A scraper `--check` can fetch the index page;
the Beacon `make check-source` check only probes source metadata/local state.

Progress-email credentials live outside the repository under `/etc/ojs-api`,
are provided through systemd credentials, and are not required for the API.
Email/optional AI reporting has separate timers and access permissions. See
[reporting limitations](OPERATIONS.md#optional-progress-email).

### Optional live-API observations in progress reports

| Setting or file | Meaning |
| --- | --- |
| `/etc/ojs-api/progress-api.env` | Root:root mode 600, created by `scripts/configure_progress_api.py`; contains the monitoring origin only, not credentials |
| `OJS_PROGRESS_API_BASE_URL` | Opt-in public HTTPS origin, with no username, password, path, query or fragment; TLS verification and redirect refusal protect the credential |
| systemd credential `api-client.env` | Private copy of the existing API username/key provided by `deploy/ojs-api-progress-email-api.conf`; never an environment-variable key or command-line secret |

No API requests are made when this origin is unset. Configure it independently
of `.env`: the reporting sandbox intentionally cannot read `.env` or `.secrets`.
The system manager supplies only the selected credential before sandboxing;
the base service's filesystem and Docker restrictions remain intact. Install
the drop-in for both the primary report service and the subscriber template
when those services are in use. No timer or recipient is changed by this opt-in.

Use `scripts/preview_progress_report.sh` for a factual sandboxed preview without
mail, AI calls or state writes. API credential failure, a network/TLS error, an
unconfigured probe and a failed update job are separate observations; none
alone proves that the serving database was lost. Observations are made from the
VM, not an external uptime provider. See the
[activation instructions](OPERATIONS.md#observe-the-live-api).

### Reporting cadence and processing-start notices

Both the primary and subscriber reporters default to a 72-hour minimum between
accepted messages. Their systemd timers use 72 hours after service completion;
reboots delay the next send rather than replaying a backlog. Each recipient has
independent durable state and shares the reporting/AI-budget lock.

The opt-in `OJS_PROGRESS_START_NOTICE_FILE` points to a bounded root-owned JSON
marker generated by the processing-start watcher, never by a download. Configure
it through `/etc/ojs-api/processing-start.env`, not the application's `.env`.
`OJS_PROGRESS_START_SUBSCRIBER` selects an existing subscriber instance name,
not an email address. No new credential or paid cloud service is required.
See [the processing-start workflow](OPERATIONS.md#one-processing-start-notice-then-three-day-reports)
for installation, arming, receipt retention and failed-delivery handling.

## Validate changes safely

On the VM, as the service account:

```bash
sudo -u ojs -H bash -lc 'cd /srv/ojs_api && make lightsail-preflight'
sudo -u ojs -H bash -lc 'cd /srv/ojs_api && docker compose config --quiet'
```

Preflight is read-only apart from normal command/system activity; it does not
prove disk/RAM peak, a valid certificate, source credentials, or healthy data
endpoints. Test those separately. Never publish rendered Compose configuration,
`.env`, private credential contents, or a full environment dump in diagnostics.
