# Transfer and publish a completed history release

Publish one completed catalogue containing all processed snapshots and events.
Do not independently build each snapshot then concatenate exports: stable IDs
and deltas depend on chronological processing. The latest clean export carries
the cumulative history; serving does not need one database per snapshot.

## Prerequisites

- A cleanly stopped, validated source release and its latest compressed clean
  export/checksum; small changes reports/checksums and release manifests for
  **every** expected snapshot.
- Matching builder, candidate and serving MySQL patch versions.
- The same reviewed helper code on workstation and server, with the server at
  `/srv/ojs_api`, an `ojs` account and completed TLS/API configuration.
- Enough measured destination space for the physical database, export,
  transfer intermediates, any retained rollback data and at least 20 GiB reserve.
- Automatic updates paused, no competing writer, and a verified SSH host key.
  The orchestrator requires noninteractive SSH and the remote privileges needed
  by its `sudo -n` commands. Docker-group access is root-equivalent.

Do not put destination credentials or private keys in Git. Do not use
`StrictHostKeyChecking=no`. Verify host fingerprints through an independent,
trusted console before adding a new host to `known_hosts`.

## Run the coordinated handoff

Substitute the actual reviewed paths, full chronological history list and HTTPS
origin. These are **workstation** commands; `--host` is mandatory so code cannot
select someone else's server.

```bash
python3 scripts/deploy_completed_release.py \
  --source-clean /srv/ojs-workstation/clean \
  --source-raw /srv/ojs-workstation/raw \
  --credentials-env /srv/ojs-workstation/credentials/local.env \
  --identity-file /home/user/.ssh/id_ed25519 \
  --host ubuntu@api.example.org \
  --snapshot 2026-10-01 \
  --history 2026-01-01 --history 2026-07-01 --history 2026-10-01 \
  --public-base-url https://api.example.org --reserve-gib 20
```

Repeat `--source-raw` if the source build used multiple raw directories, so each
receives its lock. Use `--native-only-source` only after confirming the source
has never been Docker-managed; otherwise leave Docker inspection enabled. Run
in a durable desktop service with a sleep inhibitor, graceful shutdown and
sufficient runtime; a bare interactive invocation is not disconnect-proof.

The coordinator locks and checks the stopped source; copies the selected
database, latest export and small history artifacts into a dated incoming
directory; verifies and installs them; privately ships the temporary builder
credential over SSH; then queues `ojs-api-publish-YYYY-MM-DD.service` on the VM.
That separate job adopts server credentials, publishes without pruning, and
verifies history over loopback and trusted public HTTPS.

`publication_started` means the VM job was queued, **not that publication
succeeded**. A failed transfer stops the chain. Do not run a second transfer or
publisher alongside it. If launch acknowledgement is lost, inspect the named
remote service before retrying; it may already be running.

## Accept the deployment

On the server, substitute the selected release date:

```bash
sudo systemctl status ojs-api-publish-2026-10-01.service --no-pager
sudo journalctl -u ojs-api-publish-2026-10-01.service -n 80 --no-pager
```

Check authenticated history independently (the credential file is read locally,
not printed or sent as a shell argument):

```bash
cd /srv/ojs_api
sudo -u ojs python3 scripts/check_api_history.py \
  --base-url https://api.example.org \
  --credentials-file .secrets/api-client.env \
  --snapshot 2026-01-01 --snapshot 2026-07-01 --snapshot 2026-10-01 \
  --reports-dir data/clean
```

Require agreement between snapshot rows, reports and event bounds; a legitimate
zero-change release need not contain invented events. Test REST and OAI requests
using [the API guide](../HOW_TO_CALL.md).

Automatic builds remain paused after handoff. Review capacity and retention
separately before enabling the daily schedule. `--finish-only` is for interrupted
transfer **installation**, not arbitrary failed publication. Keep source and
rollback artifacts until acceptance; transfer success does not authorize deletion.
