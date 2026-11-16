# Deployment guide

This repository contains application code, SQL transformations, deployment
templates and tests. It does **not** contain the Beacon dataset, a ready-made
database, production credentials, or a publicly usable API key. A fresh clone
needs an authorised source credential or a validated completed release.

Clone the canonical source from
[crahal/ojs-beacon-api](https://github.com/crahal/ojs-beacon-api)
using `https://github.com/crahal/ojs-beacon-api.git`.
The documented VM installation directory remains `/srv/ojs_api`; the repository
name does not change service paths or data locations.

## Choose a deployment route

| Route | Suitable when | Required working space |
| --- | --- | --- |
| Build elsewhere, serve on a small VM | A larger Linux machine can prepare the catalogue | Latest stopped database, latest clean export, audit reports and transfer reserve on the VM |
| Build and serve on the VM | A complete representative build has established capacity | Existing live release **plus** new input, candidate database, staging/index space, export and safety reserve |

Use [the Lightsail installation guide](LIGHTSAIL_DEPLOYMENT.md) for either
route. It labels commands by execution location and covers a fresh Ubuntu
host, credentials, HTTPS, publication and acceptance checks. Completed results
can be transferred without rebuilding them on the serving host.

A 4 GiB host is the configured low-memory target, **not a guarantee that every
full-data build fits or finishes quickly**. Preflight checks configuration and
current headroom; it does not predict peak storage/RAM. One completed catalogue
required approximately 227 GiB of apparent database/export space. A 512 GB disk
can hold that serving package, but this does not prove a subsequent full build
fits beside it. Keep automatic updates paused until capacity is established.

## Deployment sequence

1. Review data access rights, capacity, downtime and a recovery plan.
2. Provision persistent storage; restrict SSH and keep MySQL/the application
   port off the public network.
3. Install matching host/container MySQL patches, Docker Compose and Caddy.
4. Create private configuration and credentials; run preflight.
5. Configure trusted HTTPS on a stable public IP **or** DNS name.
6. Import a stopped completed release, or run a capacity-approved initial build.
7. Verify authenticated data, expected history, anonymous rejection, TLS,
   resource headroom and recoverable backups.
8. Enable daily source checks/builds only after reviewing next-build capacity.

Use `sudo ./scripts/install_automatic_updates.sh` to stage the updater **paused**
without starting it. Set a reviewed `OJS_BUILD_WORKSPACE_GB` budget (and scratch
budget if configured), then use `make automatic-readiness` as the deployment
account. Follow the [separate activation checklist](docs/OPERATIONS.md#activate-only-after-review)
only after storage, backups, service overrides and readiness have been reviewed.

Do not start `docker compose up` against an absent `mysql-live` pointer: MySQL
could initialise an empty database. The publication coordinator owns directory
selection, cold validation, the switch and rollback on failed checks.

## Documentation map

- [Lightsail deployment](LIGHTSAIL_DEPLOYMENT.md): ordered installation and launch.
- [Configuration reference](docs/CONFIGURATION.md): settings, credentials and limits.
- [Operations runbook](docs/OPERATIONS.md): monitoring, cron, failures, backups,
  application updates, retention and credential rotation.
- [API client guide](HOW_TO_CALL.md): authentication, queries, pagination and deltas.
- [Processing guide](PROCESSING_GUIDE.md): pipeline and change-report semantics.
- [Completed-history handoff](docs/API_HISTORY_HANDOFF.md): additional history checks.

## Service boundaries

Publication involves a brief MySQL/API restart; this is a single-host service,
not a highly available cluster. A successful source check or running service
is not proof of a usable API. Publication must pass authenticated checks.

The API exposes current article state and cumulative change events, not a
complete versioned archive of every historical payload. Preserve authorised raw
snapshots off-host if exact replay is required; local retention is bounded.

Templates are examples to review, not permission to overwrite host configuration,
format a populated disk, bypass release guards, or remove an old backup.
