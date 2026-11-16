# How the bibliometric data is processed

In plain English: **read each dated Beacon snapshot, identify which source rows
describe the same article, compare with the previous release, record what
changed, and publish only after validation.**

This guide explains the processing rules. For installation and day-to-day
operation, start with [deployment](HOW_TO_DEPLOY.md); for queries, see the
[API client guide](HOW_TO_CALL.md). The repository contains code and synthetic
test fixtures, not the real Beacon dataset or access credentials.

## The two kinds of record

A **source row** is a record observed at a journal/book endpoint. An **article**
is the stable API entity created by grouping matching source rows. One article
may have several source aliases.

For example, two endpoints can report the same DOI. They can become one
article, while both aliases remain available as provenance. If one endpoint
later disappears, the article remains active while another active alias
survives. A missing source row is therefore not automatically a deleted article.

## The processing sequence

1. **Check for a new snapshot.** The coordinator probes the authenticated Beacon
   endpoint, comparing its HTTP validators with the locally validated input.
   Unchanged upstream data does not require a new download. An unpublished
   local build or missing serving release may still require work.
2. **Validate the download.** Stream the gzip, check its integrity, hash the
   compressed and decompressed bytes, and read the SQL completion footer.
   The footer determines the snapshot date, not the download time or HTTP
   Last-Modified header. A changed file with the same snapshot date stops for
   review rather than silently replacing history.
3. **Build in date order.** Import the compressed snapshot into a separate
   candidate MySQL directory. Import the previous clean release to carry IDs,
   aliases, tombstones, and events forward. A newly found snapshot older than
   processed history requires a controlled history rebuild; it is not silently
   inserted out of order.
4. **Select and normalize records.** Join source records to their context,
   endpoint, and ISSN; include contexts whose ISSN normalizes to eight
   digits/`X` characters. Extract bibliographic fields from OAI/DC XML and
   normalize the matching keys. Hash source XML to detect edits even when the
   upstream timestamp did not change.
5. **Deduplicate and choose a source.** Join records connected by eligible exact
   keys. Preserve existing published article IDs where possible, retain merge
   tombstones, and choose one canonical source to supply the main metadata.
   All retained aliases remain traceable.
6. **Compare with the previous release.** Record additions, modifications,
   removals, restorations, and merges. Keep missing/inactive aliases and article
   tombstones instead of physically deleting their identities.
7. **Validate and report.** Check identity assignments, counts, hashes, and
   provenance. Write a checksummed change report. Unexpected large losses or
   merges quarantine the candidate for human review. A completed release gets
   its manifest only after its database, clean export, report, and checksums
   are ready.
8. **Publish, then clean up.** The compact coordinator briefly stops serving
   containers, switches the database directory, restarts, and tests the
   authenticated API. Failed checks attempt to restore the previous release;
   an interrupted switch retains its recovery journal for inspection.
   By default, eligible older large artifacts are removed only after successful
   publication, unless retention was explicitly requested. The optional
   [early export retirement](#optional-early-export-retirement) policy can remove
   the previous compressed SQL export sooner, but never the still-serving
   database.

The first snapshot establishes the baseline. Processing subsequent snapshots
against that baseline is what produces meaningful changes. Processing only the
latest dump in an empty directory does **not** reconstruct earlier history.

## Exactly how duplicates are matched

Matching is deterministic and exact, not a language model or fuzzy similarity
score. Eligible shared keys connect source aliases into groups; links can be
transitive (A matches B, and B matches C).

| Key | Rule |
| --- | --- |
| OAI identifier | Normalized endpoint identity plus lowercased source OAI identifier. |
| DOI | Extracted DOI, lowercased, with selected trailing punctuation removed. |
| Article/book URL | Recognized OJS `article/view` or OMP `catalog/book` URL; matching normalizes scheme/`www`, case, query/fragment, and trailing slash. |
| Fingerprint | Normalized title plus first creator plus publication year; requires a normalized title of at least 20 characters and nonempty creator/year. |

There are deliberate safeguards and limitations:

- Key frequency is counted across **all present aliases**, not just newly
  changed rows. Fingerprints occurring more than 25 times and other keys
  occurring more than 1,000 times are excluded from matching.
- Corrected source keys retire their old values from future matching when no
  retained eligible alias still supplies them.
- When groups meet, established published IDs take priority over new aliases.
  If established articles merge, the lowest previously published article ID
  survives; losing IDs become permanent merge tombstones.
- There is no automatic split of a previously published merge after a later
  correction. That needs a reviewed rebuild and a downstream resynchronization
  plan.
- Exact matching can still merge unrelated records if upstream identifiers or
  normalized fingerprints are wrong. It can also miss genuine duplicates.
  Frequency guards and reports reduce risk; they do not prove identity.
- The ISSN check is normalization/length validation, not verification of the
  ISSN checksum, journal quality, or publication authenticity.

The canonical source is ranked by: active first, present first, DOI present,
title/creator/year present, longer title, then lowest source ID. It is one
selected source, not an LLM-written synthesis of conflicting metadata.

## What counts as a change?

| Event | Meaning |
| --- | --- |
| `added` | A newly observed active article. |
| `modified` | API-visible metadata, canonical source, or source provenance changed. |
| `removed` | No active alias remains; the ID is retained as a tombstone. A newly observed already-inactive article can also produce this event. |
| `restored` | A previously removed article becomes active again. |
| `merged` | An article becomes a tombstone pointing to the surviving ID. |

Lifecycle dates are **snapshot observation dates**, not publication dates.
No change is inferred between snapshots that were never collected. Reprocessing
unchanged input is not supposed to create another set of events.

Each event records its stable article ID, snapshot date, version, operation,
before/after hashes, and reason. The current database retains event history
from previous processed snapshots even when their old exports are pruned.

Important: `/changes` combines historical **events** with each article's
**current payload**. It is not a complete historical archive of old titles,
abstracts, or XML. `/snapshots` lists provenance/counts, not queryable copies of
each historical database. See the [cursor and bootstrap rules](HOW_TO_CALL.md#3-synchronize-changes-safely)
before building a downstream mirror.

## Reports and publication guards

Under the configured clean directory (normally `data/clean`):

| Artifact | Purpose |
| --- | --- |
| `pkpbeacon-changes-YYYY-MM-DD.json` and `.sha256` | Per-snapshot source counts, exclusions, article totals, event counts, provenance, storage profile, and release-guard result. |
| `pkpbeacon-changes-YYYY-MM-DD.quarantined.json` | Candidate exceeded a loss/merge guard; it is not an approved published release. |
| `pkpbeacon-release-YYYY-MM-DD.json` | Completion manifest binding validated release artifacts and their checksums. |
| `pkpbeacon-cleanup-YYYY-MM-DD.json` | Audit of automatically removed managed artifacts, including paths and byte counts. |
| `pkpbeacon-changes-latest.json` | Pointer to the latest completed build report; not proof that this build is currently live. |

The normal report distinguishes source rows present, absent, explicitly removed,
newly inactive, added, and changed, plus title-less/keyless records and rows
outside ISSN scope. Source categories can overlap; do not sum them as mutually
exclusive classes. Article events use one type per affected article/snapshot.

Default inter-snapshot limits are:

| Metric | Default maximum |
| --- | --- |
| Net active-source loss / previous active sources | 10% |
| Net active-article loss / previous active articles | 5% |
| Removal events / previous active articles | 5% |
| Merge events / previous active articles | 1% |

A fraction **above** its configured maximum blocks publication. These relative
guards have no previous baseline for the first snapshot. Structural and
provenance validations still apply. The corresponding configuration variables
are `OJS_MAX_ACTIVE_SOURCE_DROP_FRACTION`,
`OJS_MAX_ACTIVE_ARTICLE_DROP_FRACTION`,
`OJS_MAX_ARTICLE_REMOVAL_FRACTION`, and
`OJS_MAX_ARTICLE_MERGE_FRACTION`.

Do not increase limits merely to clear a failure. Inspect the source,
quarantined report, and change reasons first. Any deliberate override needs
operator approval and a recorded explanation. A report saying `passed` or a
completion manifest is still distinct from successful serving publication:
authenticated `GET /meta` identifies the release actually served.

## Progress: what the logs do and do not tell you

For a deployment using the supplied systemd unit:

```bash
sudo systemctl show ojs-api-update.service \
  -p ActiveState -p SubState -p Result
sudo journalctl -u ojs-api-update.service -n 50 --no-pager -o short-iso
sudo journalctl -fu ojs-api-update.service -o short-iso
df -h / /srv/ojs_api/data
```

A workstation build, transfer, or imported-release publication can use a
different unit. Inspect the unit started by that workflow; the daily updater
may be idle while another job is working.

Progress lines contain JSON with UTC `timestamp`, `stage`, `event`,
`elapsed_seconds`, and `idle_seconds`. Depending on the stage, they also show
byte counts, metadata batches, named SQL phases, deduplication pass counts,
CPU/RAM samples, and free disk space.

- `started`, `completed`, and `failed` describe one stage, not necessarily the
  whole deployment.
- A heartbeat proves the reporter is alive, **not** that SQL is advancing.
  Increasing outer `idle_seconds` can coexist with useful nested work.
- `processed_bytes / total_bytes` is a percentage for that measured stage
  only. Imported bytes do not prove rows have committed.
- A `dedup-pass` reports a completed pass and the number of changed labels;
  it does not predict how many passes remain.
- Compare CPU/I/O, memory, and disk use across observations before diagnosing
  a stall. There is no reliable universal whole-build percentage or ETA.

The progress interval defaults to 30 seconds and is configurable with
`OJS_PROGRESS_SECONDS` (1–3600). Progress events avoid printing source XML,
SQL statements, or credentials. Bounded journal retention is diagnostic
history, not the durable change audit; keep the small checksummed reports.
Other dependency errors can still contain operational details: treat all logs
as private rather than assuming universal redaction. HTTP access logs are
disabled by default; opting in with `OJS_API_ACCESS_LOG=1` records request
paths/query parameters and client addresses, separately from build progress.

## Daily checks, pauses, and interruption

The supplied cron template checks daily at 03:17 **in the server's timezone**;
the deployment guide uses UTC. Cron starts the durable systemd unit, so logging
out does not stop the job. Install the schedule only after validating resource
capacity and the first release; a fresh clone alone does not enable cron.

Coordinators, pipeline runs, and downloaders share a raw-directory
`.source-activity.lock`. A busy invocation skips source-changing work and
leaves the next scheduled check to retry: it does not queue another download
behind the active build. Do not delete lock files. Manual file copies and
direct SQL commands bypass these protections and must not run concurrently.

A regular `data/.updates-paused` marker prevents new automatic work. It does
**not** cancel a job already running. Read-only `make check-source` can still
probe upstream while work is paused. Only explicitly reviewed publication or
recovery paths bypass the pause for their named operation.

The supplied work deadline is `OJS_MAX_RUNTIME_HOURS=720` (30 days), with a
31-day outer systemd start timeout. These are safety limits, not runtime
promises. A running job does not pick up changed settings. After cancellation,
InnoDB shutdown/rollback can outlast the deadline; the service deliberately
does not force-kill it after a short timeout.

Validated metadata and `export_ready` checkpoints have specific resume paths,
but arbitrary finalization cannot be replayed safely. Failed candidates are
retained and block automatic new downloads. Do not remove the candidate, clear
a publication journal, force-kill MySQL, or use `--force-rebuild` without
reviewing the recovery procedure.
Specialized first-snapshot exact-key recovery is not a general later-snapshot
resume mechanism.

## Storage, memory, and reproducibility

Compact storage streams gzip input without writing a full expanded SQL file,
uses bounded metadata workers, compresses selected MySQL tables, and releases
temporary matching/raw tables after their last validated use. It avoids
storing another full XML payload in every intermediate matching row. These
optimizations preserve the deduplication rules and serving fields.

It is still a **full new raw database import plus prior clean state and working
tables**, not a small row-delta import. The live database must coexist with the
candidate until successful publication. A gzip's size, a small RAM limit, or
an advertised 256/512 GB disk size is not evidence that the peak will fit.
Measure apparent file sizes on the destination filesystem and leave room for
exports, staging tables, indexes, redo/undo, and the disk reserve.

Normal successful automatic updates retain the current database, the latest
managed input, one clean export needed for the next build, and small historical
reports/manifests. Eligible older managed large artifacts are pruned; unknown,
mounted, or insufficiently validated files are not treated as safe cleanup
targets. Explicit imported-release publication and `--no-prune` retain older
artifacts instead. Extra unmanaged files can therefore still consume space.

Pruning old raw dumps saves space but prevents a complete historical rerun
unless the inputs were deliberately backed up elsewhere. Git is for code,
configuration templates, and synthetic tests—not database directories, SQL
dumps, real records, passwords, or private progress reports. If a reproducible
audit is needed, retain authorized inputs outside Git and rebuild into a new,
capacity-checked clean directory with the original code/configuration. Never
run a history rebuild against the serving directory.

### Optional early export retirement

With `OJS_RETIRE_BASELINE_EXPORT=1`, the previous release's compressed SQL
export can be removed before the new export is written. This saves space that
would otherwise hold both exports at once. It requires compact storage and
`OJS_DEFER_BASELINE_PAYLOAD=1`. All retained article payloads must first be
restored into the new candidate, validation must pass, and a durable
`export_ready` checkpoint must save the report and source/build/storage details.
The previous physical database stays available for serving and rollback until
the new release passes its publication and authenticated API checks.

Before removing the exact old export, the pipeline verifies its checksum and
release details and saves a retirement receipt. Small reports, manifests, and
checksums are retained. An interrupted export resumes from the saved candidate
after rechecking its source, build settings, and database; it does not repeat
the matching/change-processing SQL or create duplicate events. Unknown or
inconsistent recovery state is kept for review, not deleted.

This option still needs a reviewed export-space budget and disk reserve.
Actual free space is checked before writing the new export; insufficient space
stops the build and retains the candidate for recovery. If the candidate cannot
be recovered, preserve the old live database and regenerate its export, or
restore a verified external backup, before rebuilding. See the
[configuration safeguards](docs/CONFIGURATION.md#source-processing-and-safeguards)
before enabling this option.

## Implementation map

- [src/download_beacon.py](src/download_beacon.py): authenticated source discovery,
  streaming download, footer/hash validation.
- [src/run_pipeline.py](src/run_pipeline.py): dated processing, metadata work,
  checkpoints, reports, validation, and exports.
- [sql/01_build_ojs_tables.sql](sql/01_build_ojs_tables.sql): normalization,
  matching, canonical selection, lifecycle state, and event construction.
- [src/compact_storage.py](src/compact_storage.py): storage-saving SQL
  transformations and audited raw-table reclamation.
- [src/compact_update.py](src/compact_update.py): locking, capacity checks,
  publication/recovery journal, API checks, and bounded artifact retention.
