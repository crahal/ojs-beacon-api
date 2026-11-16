# API client guide

The API exposes a deduplicated article catalogue, its source provenance, and
changes observed between Beacon snapshots. Use **REST/JSON** to synchronize a
database or inspect change history; use **OAI-PMH/XML** for metadata harvesting.
All catalogue endpoints require authentication and are read-only.

This guide describes the implementation, not the availability of a particular
deployment. Obtain the HTTPS origin and credentials from its operator.
`https://api.example.org` below is a placeholder; a publicly trusted HTTPS IP
address also works. Never disable certificate verification.

## 1. Connect securely

The operator supplies `OJS_API_USERNAME` and `OJS_API_KEY`. The API key is the
**HTTP Basic password**, not a Bearer token. It is separate from the database
password, database root password, and credentials used to download Beacon.
Clients need none of those other secrets.

Set the origin and username; neither is secret:

```bash
export OJS_BASE_URL='https://api.example.org'
read -r -p 'API username: ' OJS_API_USERNAME
curl --proto '=https' --fail --show-error --connect-timeout 10 --max-time 180 \
  --user "$OJS_API_USERNAME" "$OJS_BASE_URL/meta"
```

`curl` prompts for the API key without echoing it. Do not put `username:key` in
the URL or command line, paste credentials into issues, or enable shell tracing
around secrets. There is no public sign-up or OAuth flow. Operators can enable
individually revocable client accounts using a private registry or HTTPS gateway;
the original internal credential pair remains supported for monitoring and
publication. Confirm with the operator that your account is activated before
testing: merely generating a key does not enable it on a deployed API. See
[tester access management](docs/TESTER_ACCESS.md). All client accounts have the
same read-only catalogue access; these are not per-user datasets or quotas.

### Unattended clients

Use a private credential file outside the repository, rather than putting the
key in a script. For `curl`, create a dedicated netrc file using a text editor:

```bash
install -d -m 700 "$HOME/.config/ojs-api"
export OJS_NETRC="$HOME/.config/ojs-api/client.netrc"
# Create without overwriting an existing file; do not paste secrets in the shell.
(umask 077; set -o noclobber; : > "$OJS_NETRC")
chmod 600 "$OJS_NETRC"
nano "$OJS_NETRC"
```

If the create command says the file already exists, keep it and review it in
the editor. Its contents should be:

```text
machine api.example.org
  login YOUR_ASSIGNED_USERNAME
  password YOUR_ASSIGNED_API_KEY
```

Replace the machine with the origin's hostname or IP **without** `https://`, a
port, or a path. Do not use a `default` netrc entry. Protect this file as a
password and exclude it from shared backups. The examples below use:

```bash
ojs_get() {
  curl --proto '=https' --fail --show-error \
    --connect-timeout 10 --max-time 180 \
    --netrc-file "$OJS_NETRC" "$@"
}
ojs_get "$OJS_BASE_URL/meta"
```

This does not follow redirects. If the deployment moves, verify the new origin
with its operator before updating the URL and netrc entry.

## 2. REST endpoint reference

All routes below use `GET`. JSON article field names and endpoint contracts are
implemented in [src/3_build_api.py](src/3_build_api.py). Interactive `/docs`,
`/redoc`, and `/openapi.json` routes are deliberately disabled.

| Endpoint | Response and purpose |
| --- | --- |
| `/health` | Unauthenticated `{"status":"ok"}` when a database connection can execute `SELECT 1`; not proof that a release is complete or current. |
| `/` | Authenticated service name, API version, and endpoint list. |
| `/meta` | `latest_snapshot` and `high_watermark_event_id`. Use this to identify the served release. |
| `/snapshots` | `{"data":[...]}` containing snapshot provenance and counts, in ascending snapshot-date order. Not paginated. |
| `/articles` | `data`, `next_after_id`, `latest_snapshot`, and `high_watermark_event_id`. Active articles by default. |
| `/articles/{article_id}` | One article object, including its tombstone if removed or merged. |
| `/articles/{article_id}/sources` | `data` and `next_after_id` for retained source aliases, including inactive ones. |
| `/changes` | `data`, `next_after_event_id`, `latest_snapshot`, and `high_watermark_event_id`. Events after a supplied cursor. |
| `/oai` | Authenticated OAI-PMH XML; see below. |

### Current release and history

```bash
ojs_get "$OJS_BASE_URL/meta"
ojs_get "$OJS_BASE_URL/snapshots"
```

Each snapshot describes its observation date, source filename/hash, build SQL
hash, MySQL version, source/article counts, and event count. Several snapshots
can be retained in the **same current database**. `/snapshots` is not a way to
select an old database or retrieve historical article bodies.

### Browse and filter articles

```bash
ojs_get --get "$OJS_BASE_URL/articles" \
  --data-urlencode 'after_id=0' \
  --data-urlencode 'limit=100' \
  --data-urlencode 'status=active' \
  --data-urlencode 'structured_metadata=false'
```

| Parameter | Meaning |
| --- | --- |
| `after_id` | Exclusive article-ID cursor; default `0`. Results are ordered by increasing `article_id`. |
| `limit` | Default `100`, maximum `200` in the supplied configuration; the operator may lower the maximum (also lowering the default if below 100), or raise the maximum up to `1000`. |
| `status` | `active` (default), `removed`, `merged`, or `all`. |
| `changed_since` | Inclusive `YYYY-MM-DD` filter on the article's **latest observation-change date**, not its publication date or an event-history query. |
| `issn` | Normalized to eight digits/`X` characters; for example `1234-567X`. This is a format check, not ISSN checksum validation. |
| `doi` | Exact match after trimming and lowercasing. Supply a DOI, not a `https://doi.org/` URL. |
| `ids` | Comma-separated positive article IDs, at most 100 distinct IDs. |
| `structured_metadata` | Default `true`: include parsed XML metadata as arrays keyed by field name. `false` reduces payload/parse work. |
| `include_metadata_xml` | Default `false`: set `true` only if the retained canonical source XML is needed. |

Filters combine; the `after_id` and `status` filters still apply when using
`ids`. To retrieve a known tombstone directly, use `/articles/{article_id}`.

Follow `next_after_id`, keeping other filters unchanged. A JSON `null` means
there is no further page **in the release observed by that request**. Do not
use offset pagination or assume article IDs are consecutive.

```bash
ojs_get "$OJS_BASE_URL/articles/12345"
ojs_get --get "$OJS_BASE_URL/articles/12345/sources" \
  --data-urlencode 'after_id=0' --data-urlencode 'limit=100'
ojs_get --get "$OJS_BASE_URL/articles/12345" \
  --data-urlencode 'structured_metadata=false' \
  --data-urlencode 'include_metadata_xml=true'
```

The source endpoint's cursor is a **source_record_id**, not an article ID.
Sources include endpoint/context identity, normalized matching fields,
`is_present`, `is_active`, source timestamps, lifecycle dates, and metadata
hashes. Empty or malformed source XML can yield an empty `metadata` object;
not every bibliographic field is populated.

### Article lifecycle fields

| Field | Meaning |
| --- | --- |
| `article_id` | Stable ID within the continuing processed history. Treat it as an identifier, not a row count. |
| `status` | `active`, `removed`, or `merged`. |
| `merged_into_article_id` | Surviving article ID for a merge tombstone; follow redirects if the survivor is subsequently merged. |
| `canonical_source_record_id` | Source chosen to supply the article's main metadata. |
| `source_count`, `active_source_count` | Retained aliases and currently active aliases. |
| `date_added`, `date_modified`, `date_removed` | Pipeline observation dates; publication and upstream timestamps are separate fields. |
| `version_number` | Incremented for API-visible metadata, provenance, or lifecycle changes. |
| `data_hash`, `provenance_hash` | Hashes of canonical content and source-alias state, respectively. |

Some IDs can exceed JavaScript's safe integer range over time. Preserve integer
precision in JSON parsing and storage; do not round cursors or IDs.

## 3. Synchronize changes safely

```bash
ojs_get --get "$OJS_BASE_URL/changes" \
  --data-urlencode 'after_event_id=0' \
  --data-urlencode 'limit=100' \
  --data-urlencode 'structured_metadata=false'
```

Each element of `data` has three parts:

- `event`: `event_id`, `event_snapshot_date`, `event_type`, `event_operation`,
  `event_version_number`, `event_redirect_to_article_id`, before/after data and
  provenance hashes, and a structured `reason`.
- `current_operation`: `upsert` if the article is currently active; otherwise
  `delete`.
- `article`: the **current** article object, not the body at the event date.

Event types are `added`, `modified`, `removed`, `restored`, and `merged`.
An old `added` event can therefore have `current_operation="delete"` if the
article has since been removed. Use `current_operation` and `article` to maintain
a current mirror; use the `event` fields to analyze historical changes.

**This is an event audit trail, not a full before/after payload archive.** There
is no `as_of` endpoint. Keep your own versioned copies if you need historical
titles, abstracts, or complete old records.

### Durable cursor algorithm

1. Load the last committed event cursor (initially `0`). Fetch `/changes` with
   `after_event_id` equal to that value.
2. In one downstream transaction, apply each item idempotently by `article_id`:
   upsert its current article, or retain/apply a removed/merged tombstone. If
   storing events, use `event_id` as their unique key.
3. In that same transaction, save the **last returned `event.event_id`**. Commit
   before requesting another page.
4. Continue while `next_after_event_id` is non-null. On the final page it is
   null even when the page contains events: keep the last returned event ID,
   not null. An empty page leaves the durable cursor unchanged.
5. Poll again later from that saved cursor. Retry a failed request from the last
   committed cursor; never skip ahead to a new high-water mark.

Use one ordered consumer per mirror, or coordinate writes so older responses
cannot overwrite newer ones. Cursors need not be consecutive. If the operator
rebuilds/replaces history, arrange a full resynchronization; cursors and IDs are
not a cross-deployment identifier scheme. A high-water mark lower than your
saved cursor is a reason to stop and ask the operator, not silently reset it.

### Bootstrap a current mirror efficiently

For a new, empty mirror:

1. Read `/meta` and save its `high_watermark_event_id` as `H` **before** starting.
2. Scan `/articles?status=all&after_id=0` to exhaustion, storing articles and
   tombstones. Save scan progress separately from the event cursor.
3. Consume `/changes?after_event_id=H` using the transaction procedure above.
   This reconciles changes published while the article scan was running.

Do not bootstrap into a non-empty mirror without an explicit reconciliation
strategy. Pages, metadata, and their watermarks are separate reads, not a
transactionally frozen export. Publication may briefly interrupt connections;
retry safely. Ask the operator for a consistent export if you need an exact
single-release analytical dataset.

## 4. OAI-PMH harvesting

The `/oai` route supports all six verbs, with `oai_dc` as its only metadata
format. Supply parameters with URL encoding:

```bash
ojs_get --get "$OJS_BASE_URL/oai" --data-urlencode 'verb=Identify'
ojs_get --get "$OJS_BASE_URL/oai" --data-urlencode 'verb=ListMetadataFormats'
ojs_get --get "$OJS_BASE_URL/oai" --data-urlencode 'verb=ListSets'
ojs_get --get "$OJS_BASE_URL/oai" \
  --data-urlencode 'verb=GetRecord' \
  --data-urlencode 'metadataPrefix=oai_dc' \
  --data-urlencode 'identifier=oai:pkp-beacon:article:12345'
ojs_get --get "$OJS_BASE_URL/oai" \
  --data-urlencode 'verb=ListIdentifiers' --data-urlencode 'metadataPrefix=oai_dc'
ojs_get --get "$OJS_BASE_URL/oai" \
  --data-urlencode 'verb=ListRecords' --data-urlencode 'metadataPrefix=oai_dc'
```

`ListIdentifiers` and `ListRecords` accept inclusive `from` and `until` dates
(`YYYY-MM-DD`), and an ISSN set such as `set=issn:1234567X`. These dates select
the article's **latest API-visible modification**, not publication dates or all
events that occurred during that interval. A record modified later will not be
returned as its earlier historical body.

`ListSets`, `ListIdentifiers`, and `ListRecords` paginate with an opaque
`resumptionToken`. Pass it unchanged, with only the same verb:

```bash
read -r -p 'Returned resumption token: ' OJS_TOKEN
ojs_get --get "$OJS_BASE_URL/oai" \
  --data-urlencode 'verb=ListRecords' \
  --data-urlencode "resumptionToken=$OJS_TOKEN"
```

Do not repeat `metadataPrefix`, dates, or set filters when using a token. An
empty token ends the harvest. The default page size is 50, configured by the
operator. A token is a cursor, not a pinned-release snapshot. For incremental
date-based harvests, use overlapping day boundaries and deduplicate by stable
identifier; prefer REST event cursors when exact change accounting matters.

OAI responses use `text/xml` and namespace
`http://www.openarchives.org/OAI/2.0/`. **Parse `<error>` even after HTTP 200**:
`noRecordsMatch` means an empty selection, while `badArgument`,
`cannotDisseminateFormat`, `badResumptionToken`, and other errors need explicit
handling. Checking only the HTTP status is insufficient.

Removed and merged articles have persistent deletion headers with
`status="deleted"`, an identifier, and datestamp; no metadata body is included.
Tombstone the identifier downstream. OAI does not include merge redirects: use
the corresponding REST article to obtain `merged_into_article_id`.

## 5. Limits, errors, and responsible use

Start with a single sequential harvester on a small deployment. Defaults are a
single API worker, an eight-connection concurrency limit, REST pages of 100
(maximum 200), and OAI pages of 50. These are global resource limits, **not** a
per-client requests-per-second quota. No per-client rate limiter is implemented.
Avoid parallel full-catalogue scans or repeatedly downloading XML unnecessarily.

| HTTP status | Client action |
| --- | --- |
| `401` | Check Basic username/key; do not retry indefinitely. |
| `404` | Unknown article or route. A retained removed/merged article normally returns its tombstone, not 404. |
| `422` | Correct invalid REST dates, status, ISSN, IDs, or pagination parameters. |
| `503` | Service unavailable, concurrency limit reached, or no published snapshot; retry later. |
| Other `5xx`, timeouts, connection failures | Retry idempotent GET requests with bounded exponential backoff and jitter. |

For example, wait roughly 2, 4, 8, 16, then 30 seconds, with small random jitter;
stop after a bounded number of attempts and alert rather than hammering the
server. Honor `Retry-After` if an upstream proxy supplies it. Never advance a
cursor on an unsuccessful or unparsed response.

When reporting an issue, share the endpoint, non-secret parameters, UTC time,
HTTP status, and `/meta` snapshot date if available. Do not share the key,
Authorization header, private configuration, or an unrestricted verbose HTTP
trace. API request access logs are disabled by default; operators can opt in
with `OJS_API_ACCESS_LOG=1`, which records client addresses and request paths
including query parameters. Keep enabled access logs and other diagnostics
private; do not assume every third-party exception is redacted.
Operators should follow [deployment](HOW_TO_DEPLOY.md) and
[processing](PROCESSING_GUIDE.md) instructions.
