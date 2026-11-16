# Share source without operational data or history

The canonical public source is
[crahal/ojs-beacon-api](https://github.com/crahal/ojs-beacon-api), with a fresh
source-only history. For routine contributions, clone that repository and follow
[the contribution workflow](../CONTRIBUTING.md). Never mirror or push private
operational history into the public repository.

The export workflow below is for reviewing code-only source snapshots from a
checkout that may also hold private deployment notes. It creates a **new
code-only archive**, not a mirror, fork, Git metadata backup or history rewrite.
Keep operational checkouts private and preserve their remotes while work is active.

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

## Optional: initialise a separate reviewed snapshot

This is for a deliberately separate source-snapshot repository, not updating
the canonical repository or replacing its established history. Normal changes
belong on a branch of a fresh canonical clone. If a new standalone snapshot is
required, extract into a **new** directory, never over the live project:

```bash
OJS_SHARE_DIR=$(mktemp -d /tmp/ojs-api-public.XXXXXXXX)
tar -xzf dist/ojs-api-source.tar.gz -C "$OJS_SHARE_DIR"
cd "$OJS_SHARE_DIR/ojs_api"
git init -b main
git config user.name 'Project maintainer'
git config user.email 'maintainer@example.org'
git add .
python3 scripts/check_repository.py
git diff --cached --stat
git commit -m 'Publish reviewed code-only source'
python3 scripts/check_repository.py --history
```

Choose a suitable public author identity before committing; `example.org` above
is a placeholder, or use your actual GitHub **noreply** address. Attribution and
GPL notices in source/LICENSE must remain intact. No remote is added or pushed
by these commands. Create/review the intended GitHub repository separately before
connecting it, and inspect visibility, Actions permissions, private vulnerability
reporting and branch protection settings. Do not force-push over collaborators'
existing history without an explicitly coordinated migration.

This new snapshot omits old history; it does not delete private information from
the old remote, forks, caches, issue attachments or other clones. If already
published, follow [GitHub's sensitive-data removal procedure](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)
and rotate exposed credentials. No scanner can promise to recall prior copies.
