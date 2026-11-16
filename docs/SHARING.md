# Share source without operational data or history

[crahal/ojs-beacon-api](https://github.com/crahal/ojs-beacon-api) is the single
maintained source repository, with clean source-only history. Normal changes
belong on branches of that repository; follow
[the contribution workflow](../CONTRIBUTING.md). Never mirror, push or merge
legacy private operational history into it. Retain any historical backup only
as protected private archival material, outside the active development workflow.

The export workflow below creates a code-only archive for review or offline
handoff, not a second maintained repository, Git metadata backup or history
rewrite. Ignored private files stay local. The Docker name `ojs-api` and VM
directory `/srv/ojs_api` are intentionally unchanged runtime names, not separate
source repositories. Pushing to the canonical repository does not deploy code;
use the reviewed [upgrade workflow](OPERATIONS.md#application-and-dependency-upgrades).

## Check the candidate and history separately

```bash
make check-repository
make check-history
```

The first scans tracked and nonignored candidate files. The second also scans
reachable historical objects and commit/tag metadata; it may fail even when
today's files are clean. Findings show type, filename and line, never the matched
secret/address. Checks are heuristic and deliberately conservative. Manually
review prose, source/test fixtures and externally stored GitHub content too.

The public repository's CI uses a full checkout and runs both checks, including
commit/tag identities. Always run the history check locally **after committing
and before pushing** as well: CI only runs after upload and cannot prevent a
private address being disclosed in the first place. GitHub-provided noreply
identities and the exact public GitHub committer/Dependabot service identities are permitted;
ordinary personal addresses are not.

Set GitHub account email privacy before opening a public PR as well as using a
noreply local Git identity. GitHub can create a temporary test-merge commit with
the account email independently of the submitted branch. If CI finds that
exposure, stop publication, correct the account setting, and review the generated
metadata again; do not weaken the allowlist. Previously published/cached metadata
is not erased by a settings change. See [the contribution checklist](../CONTRIBUTING.md)
and the sensitive-data removal guidance below.

Do not force-add data or secrets, and do not use a generic recursive zip/tar of
the working directory. `.gitignore` does not remove already tracked content,
erase history, or configure cloud-sync exclusion. Local operator notes may be
kept under ignored `.local/operations/`; this location is also excluded from
Docker and the public source exporter, but must still be protected from backups
or cloud sync according to your privacy requirements.

## Create and inspect the clean export

```bash
make export-public
tar -tzf dist/ojs-api-source.tar.gz
sha256sum dist/ojs-api-source.tar.gz
```

The exporter validates the exact bytes it archives, uses an explicit source-path
allowlist, excludes `.git` and private/data paths, refuses links and oversized
files, and normalizes timestamps, owner names and IDs. It prints a checksum and
refuses to overwrite an existing export. After changes, choose a fresh output:

```bash
python3 scripts/export_public_source.py --output dist/ojs-api-source-reviewed.tar.gz
```

Review the file listing and extracted source. Do not distribute `.local/`, the
original `.git`, backups, `.env`, credentials or generated research data alongside
the archive. The archive is mode 0600 locally; deliberately select its audience.

Extract archives into a new disposable directory for inspection, never over the
live project. If review identifies changes, apply the reviewed source diff on a
canonical branch rather than initializing or maintaining another repository.
Preserve attribution and GPL notices in source/LICENSE. An archive is not proof
that the deployed application matches the current canonical revision.

## Legacy history remains private

An export or canonical-history migration does not delete private information
from old remotes, forks, caches, issue attachments or other clones. Keep retained
historical metadata backups private and out of source exports; do not reconnect
them as an active contribution workflow. If sensitive material was already
published, follow [GitHub's sensitive-data removal procedure](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)
and rotate exposed credentials. No scanner can promise to recall prior copies.
