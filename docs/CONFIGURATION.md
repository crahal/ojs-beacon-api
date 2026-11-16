# Configuration reference

Start with [deployment](../HOW_TO_DEPLOY.md). The authoritative defaults are
[`.env.example`](../.env.example), [Compose](../docker-compose.yml) and the
[systemd update unit](../deploy/ojs-api-update.service). This reference explains
their operational meaning; changing a value does not resize an AWS resource.

## Private files

| File | Purpose | Handling |
| --- | --- | --- |
| `.env` | Host/publisher settings, including MySQL root password | `ojs` owned, mode 600; never commit or print |
| `.secrets/api-client.env` | Client HTTP Basic username/key | Mode 600; share only through a secret channel |
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
| `OJS_METADATA_WORKERS` | `1` | XML metadata workers; leave at one on a 4 GiB host |
| `OJS_PROGRESS_SECONDS` | `30` | Heartbeat interval; finite 1–3600 seconds |
| `OJS_MIN_FREE_GB` | `20` | Minimum starting free space, measured in GiB; not estimated build peak |
| `OJS_DISK_RESERVE_GB` | `8` | Stop guard while processing, measured in GiB |
| `OJS_MIN_AVAILABLE_MEMORY_MB` | `512` | Minimum currently available host memory, measured in MiB |
| `OJS_UPDATE_NICE` | `15` | Host CPU scheduling niceness; 0–19 |
| `OJS_MAX_RUNTIME_HOURS` | `720` | Coordinator work cutoff, captured at process startup |
| `OJS_MYSQL_TMP_ROOT` | Unset | Optional dedicated scratch filesystem; requires matching access/mount rules and its own reserve |
| `OJS_MAX_ACTIVE_SOURCE_DROP_FRACTION` | `0.10` | Quarantine unexpectedly large active-source loss |
| `OJS_MAX_ACTIVE_ARTICLE_DROP_FRACTION` | `0.05` | Quarantine unexpectedly large active-article loss |
| `OJS_MAX_ARTICLE_REMOVAL_FRACTION` | `0.05` | Quarantine unexpectedly large removals |
| `OJS_MAX_ARTICLE_MERGE_FRACTION` | `0.01` | Quarantine unexpectedly large merges |

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
