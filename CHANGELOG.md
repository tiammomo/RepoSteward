# Changelog

User-visible changes are recorded here. `Unreleased` describes development on main;
merging a PR or exporting a plugin does not create a GitHub Release or publish to PyPI.
See [version and release policy](docs/releases.md) for verification and upgrade steps.

## [Unreleased]

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
