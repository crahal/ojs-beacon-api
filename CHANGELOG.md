# Changelog

## Unreleased

- Consolidate maintained source and contribution instructions under
  `ojs-beacon-api`; preserve existing runtime paths and service names. Repository
  consolidation does not deploy code or alter running processing.
- Include individual tester credentials, a guarded proxy-only activation mode,
  and separate gateway/native revocation guidance. Keep invitation files and
  plaintext tester keys outside source control and deployment transfers.
- Add opt-in predecessor-export retirement only after a validated, hydrated
  successor has a durable export checkpoint. Preserve the live rollback
  database, event-ID allocation and interrupted-publication recovery.
- Add a guarded, two-stage Lightsail disk-retirement script: stop/detach/restart
  first, then deletion only after independent filesystem and API/history checks.
- Add opt-in baseline article-payload deferral and compressed event history for
  space-constrained VM builds. Preserve original values, hashes and deltas;
  require checksum-bound input and byte-exact, complete hydration before export
  or publication. Keep the existing API release online during processing.
- Add opt-in 4 KiB physical compression for new compact builds, including
  safe predecessor-export DDL conversion, layout-bound checkpoints and
  multi-snapshot lossless-equivalence tests. Default storage remains unchanged.
- Require an explicit build-workspace budget, compatible MySQL tools and a
  verified cumulative-history baseline before automatic downloads or builds.
- Add read-only automation readiness checks and a prepare-only installer that
  preserves pause controls and existing systemd overrides.
- Refuse cleanup when filesystem mounts alias protected source or live data.
- Separate authenticated live-API availability from updater history and current
  download, validation, processing and publication activity in progress reports.
- Add opt-in, receipt-tracked processing-start notices followed by independent
  three-day reporting schedules, with ambiguous delivery held for review.
- Scan full public Git history and commit identities in CI, with an explicit
  local pre-push privacy check to prevent initial disclosure.
- Replace machine-specific deployment notes with portable deployment, API,
  processing, configuration and operations guides.
- Add current-tree/history privacy checks and deterministic code-only exports.
- Exclude data, credentials, private notes and Git history from public exports
  and restrict the API Docker build context to required source files.
- Require an explicit SSH destination for private builder-credential transfer.
- Default API access logs off; sanitize health/scraper errors and validate OAI
  resumption tokens before use.
- Stop expanding the database root password into container health-check argv.
- Pin the example serving MySQL patch to the documented builder version.
- Update PyMySQL to 1.2.4, which includes upstream security fixes for parameter
  escaping; application and dependency changes require a planned image rollout.
- Pin the tested transitive Python runtime dependencies as well as direct ones.
- Add portable test/image-build CI and dependency review configuration.

These source changes do not automatically update a running server, publish a
catalogue, enable schedules, change GitHub visibility or rewrite existing history.
