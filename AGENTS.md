# Starfix contributor guidance

## Scope

Starfix discovers public GitHub issues, prepares fixes in disposable clones, and
opens pull requests only after repository-specific gates and local human review.

## Commands

- Install: `uv sync`
- Tests: `uv run python -m unittest discover -s tests -v`
- Lint: `uvx ruff check .`
- Format check: `uvx ruff format --check .`
- CLI smoke test: `uv run starfix --help`

## Safety invariants

- Never expose GitHub credentials to Codex, tests, repository hooks, or Docker
  containers. A token may be passed only to the GitHub REST client and the
  hook-disabled `git push` process.
- Do not submit a PR without a separate `submit` invocation, the
  `STARFIX_ENABLE_SUBMIT=1` environment gate, and `--reviewed-by tiammomo`.
- Respect repository contribution policies. Assignment/approval checks are hard
  gates, not ranking hints.
- Never automate security reports, mass comments, or issue creation.
- Keep public-repository tests inside the hardened verifier container.
