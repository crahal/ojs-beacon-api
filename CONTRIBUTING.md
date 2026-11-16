# Contributing

Contributions should be reproducible with synthetic fixtures, preserve existing
article identifiers and history, and keep operational data out of source control.
Read [processing](PROCESSING_GUIDE.md), [API contracts](HOW_TO_CALL.md) and
[security](SECURITY.md) before changing the pipeline or deployment.

## Start from the canonical source

[crahal/ojs-beacon-api](https://github.com/crahal/ojs-beacon-api) is the single
maintained source repository. Use a canonical working clone and a focused branch;
if you do not already have one:

```bash
git clone https://github.com/crahal/ojs-beacon-api.git ojs_api
cd ojs_api
git switch -c improve-documentation
```

Use a suitable public commit identity, such as your GitHub-provided noreply
address. Submit a pull request to
[crahal/ojs-beacon-api](https://github.com/crahal/ojs-beacon-api); contributors
without write access can fork this public repository and push their branch to
that fork.

Before opening a public pull request, also enable **Keep my email addresses
private** and **Block command line pushes that expose my email** in your
[GitHub email settings](https://github.com/settings/emails). Local Git author
settings do not control GitHub-generated test-merge commits: those may use your
account email. Inspect both branch history and the PR's generated merge metadata;
do not allow a personal address just to make CI pass. Changing settings does not
remove metadata already published or cached.

This public repository has a clean source-only history. Never mirror-push legacy
operational history, push its refs/tags, or merge unrelated private history into
it. Bring over a legacy fix as a reviewed source diff on a canonical branch,
inspect every changed file and create a new commit. Any old Git-history backup
is private archival material, not another maintained repository or a source of
branches to merge. Ignored private notes, invitations, data, credentials and
generated artifacts stay local; do not copy them or old `.git` metadata into a
contribution.

Source maintenance and deployment are separate. A push or merged PR does not
automatically deploy or restart the API or pipeline. The runtime name `ojs-api`
and installation directory `/srv/ojs_api` intentionally stay unchanged. Do not
update application files underneath an active build; use the reviewed
[upgrade workflow](docs/OPERATIONS.md#application-and-dependency-upgrades).

## Check repository status

Run these commands in the intended working clone, not in a backup or temporary
review checkout:

```bash
git remote get-url origin
git status --short --branch
git diff --stat
git diff --summary
git diff --cached --stat
```

The maintained remote is `https://github.com/crahal/ojs-beacon-api.git`.
In short status, the first column shows staged changes, the second shows
unstaged changes, and `??` marks untracked files. These are file changes, not
unpushed commits. `git diff --summary` identifies executable-permission changes
that can appear even when file contents are identical. Restore intended
permissions after review; do not hide them by disabling Git's file-mode checks.

For a branch with an upstream configured, refresh the remote references and
compare commits:

```bash
git fetch origin
git rev-list --left-right --count 'HEAD...@{upstream}'
```

The first count is local-only commits; the second is upstream-only commits.
`0 0` means the branch histories match, but does not mean the working files are
clean. Counts before a fetch reflect the last observed remote state. A new
branch without an upstream cannot use this comparison yet.

If an editor disagrees with these commands, check its selected repository and
refresh Source Control. Close temporary review checkouts in the editor; reload
the window if necessary. Do not delete files, force-add ignored data or reset
work to make an editor badge disappear. Git status and synchronization are
separate from API availability and processing progress; fetching or committing
source does not deploy it.

## Local checks

Use Python 3.12 in an isolated environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
make check-repository
make check-history
make test
git diff --check
```

No Beacon account or research data is needed for tests. Native integration tests
are skipped when the MySQL toolchain is absent. For pipeline/storage changes,
also run the suite with matching MySQL 8.4.11 `mysqld`, `mysql`, `mysqladmin` and
`mysqldump` available on PATH. Confirm those tests executed rather than skipped.
Use a disposable environment: fixtures initialize temporary databases and need
permission to start local processes/sockets. Do not use the production datadir.

CI scans the source tree and reachable public Git history, including commit/tag
metadata. Configure a suitable public identity **before committing**, and run
`make check-history` again after creating your commit and before pushing. A CI
failure happens after upload; it cannot prevent an initial disclosure or erase
data already sent to GitHub.

CI also runs the portable test suite in a Python container without MySQL, builds
the API image separately, and checks known dependency advisories. It does not
establish full-data capacity, launch AWS
resources, exercise production TLS, or certify a production release. A passing
unit suite is not a substitute for native integration and measured release guards.

## Change review

- Keep changes focused; add regression tests for the failure being fixed.
- For deduplication, test collisions, corrected keys, removals/restorations,
  stable identities and chronology. Do not silently redefine historical events.
- For SQL or storage changes, test fresh builds and supported recovery paths.
  Document checkpoint/schema/version compatibility and a safe rollback plan.
- For deployment changes, preserve pause markers, locks, reserves and credential
  separation. Never add destructive cleanup to an automatic error handler.
- Keep commands/documentation aligned with actual argument parsers, defaults and
  response schemas. Use `example.org`, RFC 5737 IPv4 or `2001:db8::` IPv6 examples,
  not personal machines or real administrator addresses.
- Never attach `.env`, data, database exports, secrets, raw operational logs or
  screenshots containing infrastructure identifiers. Use minimal synthetic cases.
- Run `make check-history` before sharing existing history. Review commit author
  identity; use a GitHub-provided noreply address if personal email privacy matters.

Do not commit generated databases or run large upstream downloads in CI. Keep
dependency updates explicit and tested; update documentation when required tool
versions change. Contributions are distributed under the repository's [GPL-3.0 license](LICENSE).
