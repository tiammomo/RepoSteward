# Changelog

User-visible changes are recorded here. `Unreleased` describes development on main;
merging a PR or exporting a plugin does not create a GitHub Release or publish to PyPI.
See [version and release policy](docs/releases.md) for verification and upgrade steps.

## [Unreleased]

- Live task context and scoped knowledge reuse one workspace observation per read
  response. Authority changes reject mixed reads; writes and verifier phases keep
  independent fresh observations. No snapshot cache survives the response.

- Task handoff export, checkpoint creation and verification reservation observe source
  outside SQLite writer transactions, then compare revision, scope and evidence before
  atomic publication. Concurrent changes and idempotent retries retain their checks.

- Workbench preparation reuses verified bundles after backend changes that preserve
  OpenAPI, skips unchanged API type generation, and shares TypeScript incremental
  checks between build and test. CI caches lock-bound npm downloads. Changed source
  during preparation and corrupt assets still invalidate the result.

- Core source, editable and wheel builds no longer invoke npm or require web dependencies.
  Prepare workbench assets explicitly with `scripts/build_frontend.py` and package them
  with `REPOSTEWARD_BUILD_WEB=1`; HTTP dependencies are available through the `web` extra.

The current source baseline is **0.1.0**. No historical release date is inferred from
that version string. This initial log summarizes the current baseline; it is not a
claim that every historical change is listed.

### Agent assistance

- Read local projects through a source-linked code guide and bounded evidence queries.
- Keep task context, checkpoints and verification evidence outside coding sessions.
- Export four Codex skills and a workspace/account-pinned MCP connection through
  `plugin plan` and a separately reviewed `plugin export`.
- Use `reposteward` as the default plugin instance name; use `reposteward-<project>`
  for additional workspaces. Existing instance names remain supported.

### Delivery and maintenance

- Archive deterministic benchmark reports per tested commit and workflow attempt for 30 days;
  preserve failure reports and document offline baseline comparison.

- Derive package metadata and runtime version from `src/reposteward/__init__.py`.
- Check wheel/sdist versions and packaged source/resources against the checkout before release;
  print artifact SHA256 values and optionally validate a release tag/changelog pair.
- Include Chinese onboarding and the changelog in the source distribution.
- Document initial installation, migration from `reposteward-local`, upgrades and rollback.
- Preserve reviewed Issue-to-PR, isolated verification, and separate publication gates.

### Limits

- Generated plugins bind one local workspace and contain machine-specific paths.
- Codex plugin packaging does not establish Claude Code/Copilot plugin compatibility.
- FastAPI/React workbench changes on open PRs are not part of this baseline.
- Token collection does not yet establish a measured token-savings percentage.
