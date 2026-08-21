# RepoSteward contributor guidance

## Scope

RepoSteward discovers public GitHub issues, prepares fixes in disposable clones, and
opens pull requests only after repository-specific gates and local human review.

## Commands

- Install: `uv sync`
- Tests: `uv run python -m unittest discover -s tests -v`
- Lint: `uvx ruff check .`
- Format check: `uvx ruff format --check .`
- CLI smoke test: `uv run reposteward --help`

## Safety invariants

- Never expose GitHub credentials to a coding harness, tests, repository hooks,
  Git push, or Docker containers. An API credential may be passed only to the
  GitHub REST client; Git clone/push uses the host's SSH key.
- Do not submit a PR without a separate `submit` invocation, the
  `REPOSTEWARD_ENABLE_SUBMIT=1` environment gate, and a `--reviewed-by` value
  matching the configured GitHub login.
- Respect repository contribution policies. Assignment/approval checks are hard
  gates, not ranking hints.
- Never automate security reports or mass comments. Repository Issue creation must
  pass through the configured online Project draft, a fresh duplicate/security
  review digest, a distinct reviewer, and the separate promotion gate.
- Keep public-repository tests inside the hardened verifier container.
