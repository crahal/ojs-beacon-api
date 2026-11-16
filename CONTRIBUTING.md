# Contributing

Contributions should be reproducible with synthetic fixtures, preserve existing
article identifiers and history, and keep operational data out of source control.
Read [processing](PROCESSING_GUIDE.md), [API contracts](HOW_TO_CALL.md) and
[security](SECURITY.md) before changing the pipeline or deployment.

## Start from the canonical source

Create a fresh working clone and a focused branch:

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

This public repository has a clean source-only history. Never mirror-push an
operational repository, push all of its refs/tags, or merge unrelated private
history into it. To bring over a fix, apply only the reviewed source diff in
this fresh clone, inspect every changed file and create a new commit. Do not
copy `.git`, private notes, data, credentials or generated artifacts. Leave an
active production/workstation checkout and its remote untouched.

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

CI runs the portable test suite in a Python container without MySQL and builds
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
