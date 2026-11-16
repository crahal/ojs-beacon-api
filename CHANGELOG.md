# Changelog

## Unreleased

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
