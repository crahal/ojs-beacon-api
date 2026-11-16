# OJS Beacon API

A reproducible pipeline for turning PKP Beacon snapshots into a deduplicated
bibliometric catalogue, with an authenticated, read-only REST and OAI-PMH API.
The latest catalogue carries stable article identifiers, source provenance,
snapshot summaries and a cumulative change feed.

The single maintained source repository is
[crahal/ojs-beacon-api](https://github.com/crahal/ojs-beacon-api).

This repository contains **code, configuration templates and documentation
only**. It does not include datasets, credentials, a prebuilt database or a
public API account. Obtain authorized upstream access separately. Starting an
empty MySQL container does not create a usable catalogue.

## Start here

| Task | Guide |
| --- | --- |
| Deploy a fresh server | [Deployment overview](HOW_TO_DEPLOY.md), then [Lightsail setup](LIGHTSAIL_DEPLOYMENT.md) |
| Query or harvest the API | [API guide](HOW_TO_CALL.md) |
| Understand processing and deduplication | [Processing guide](PROCESSING_GUIDE.md) |
| Monitor, update, back up and recover | [Operations](docs/OPERATIONS.md) |
| Choose settings and resource limits | [Configuration](docs/CONFIGURATION.md) |
| Build history on a separate workstation | [Workstation guide](WORKSTATION_RECOVERY.md) |
| Transfer and publish completed history | [Release handoff](docs/API_HISTORY_HANDOFF.md) |
| Review a storage migration | [Storage migration](docs/LIGHTSAIL_512_STORAGE.md) |
| Contribute or report a security concern | [Contributing](CONTRIBUTING.md), [security](SECURITY.md) |
| Share a privacy-checked source snapshot | [Sharing checklist](docs/SHARING.md) |

## How it works

1. Check the authorized Beacon endpoint for a new snapshot. Unchanged data does
   not trigger a download or rebuild.
2. Validate and stream the compressed dump into an isolated MySQL candidate.
   Import the previous clean state to carry identities and history forward.
3. Parse new or changed metadata, reconcile conservative identity keys, and
   report additions, modifications, removals, restorations and merges.
4. Validate the candidate and review publication guards. Keep the currently
   served database untouched while building.
5. Publish the completed candidate, check the API, and retain the clean export
   and audit reports according to the documented retention policy.

Each snapshot must be processed chronologically against its predecessor to
produce meaningful deltas. The change feed is **not a full archive of every
historical article payload**; article endpoints return current catalogue state.
Merges are conservative, deterministic decisions, not probabilistic guarantees
that every duplicate is found. Published merges are not automatically split.

## Deployment model

The Docker service/container name `ojs-api` and installation directory
`/srv/ojs_api` intentionally remain unchanged. They are runtime names, not
separate maintained repositories. Pushing source changes does not deploy them;
application upgrades follow the reviewed [operations workflow](docs/OPERATIONS.md#application-and-dependency-upgrades).

The reference deployment uses Ubuntu 24.04, Python 3.12, matching MySQL 8.4.11
builder/serving tools, Docker Compose and a TLS reverse proxy. A 4 GiB host is
the resource-limited serving target; a larger workstation can do the expensive
builds. Match the **exact MySQL patch** when moving physical database files.
Review and test dependency upgrades rather than changing to floating tags.

Storage must cover the live database, recovery export, new compressed input,
candidate, staging, temporary files and safety reserves. Neither 256 GB nor
512 GB is a blanket guarantee for simultaneous full-data building and serving.
Measure a complete build before enabling automatic publication on a small VM.

The daily cron template starts a bounded systemd service. Shared workflow locks
prevent repository download and processing entry points from overlapping, even
if a build lasts several days. A pause marker stops future automatic work.
**Scheduling is opt-in**, after the first successful release and capacity review.
Never copy raw data manually into a directory being processed.

## Security and privacy

All data routes require HTTP Basic API credentials over trusted HTTPS. The
serving database account has SELECT-only access, MySQL has no public port, and
the API container binds to loopback behind the reverse proxy. Credentials and
data stay outside Git. API credentials are distinct from source, database, SSH,
email and optional OpenAI credentials.

Only `/health` is an unauthenticated application route. Its success verifies
database connectivity, not the existence of a complete published catalogue;
deployment acceptance also checks authenticated metadata, snapshots and changes.
See [security boundaries and limitations](SECURITY.md).

Public examples use documentation-only addresses and domains. Ignored private
files stay local: do not publish operator notes, invitations, raw logs, `.env`,
`.secrets` or data directories. Legacy operational Git history is not merged
into the public repository; any retained historical backup is private archival
material, not an active development repository. The repository checker is a
useful guard, **not a complete secret scanner or security certification**.

## Development

Use a disposable environment, not the production checkout or database:

```bash
git clone https://github.com/crahal/ojs-beacon-api.git ojs_api
cd ojs_api
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
make check-repository
make test
```

Tests use synthetic data and temporary directories. Native MySQL integration
tests run when the required executables are available; otherwise they are
explicitly skipped. Run those tests against the documented MySQL version before
shipping pipeline changes. CI runs the portable suite, checks known dependency
advisories and builds the API image;
it does not download Beacon data or deploy to AWS.

Develop on branches of the canonical repository. Do not push or mirror legacy
operational history into it; bring over only reviewed source changes and follow
[the contribution workflow](CONTRIBUTING.md). Code-only exports are for review or
offline handoff, not a second maintained repository.

## License and data rights

The code is licensed under [GPL-3.0](LICENSE). This does not grant rights to
redistribute upstream dumps, article metadata or full text. Follow the upstream
provider's access terms and applicable data obligations. Do not put research
data in Git releases, issues, test fixtures or container images.
