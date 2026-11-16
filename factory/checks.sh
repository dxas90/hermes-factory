#!/usr/bin/env bash
# The repo's checks. Exit 0 only when the branch is fit to review.
#
# The gate runs this script from the main checkout with the job's worktree as
# the working directory. Keep it fast, deterministic and non-interactive: no
# watch modes, no prompts, nothing that rewrites files.
#
# For the hermes-factory repo itself the checks are its own test suite.
# Other repos: replace the last line with this project's real commands, e.g.:
#
#   npm ci --prefer-offline
#   npm run lint
#   npm run typecheck
#   npm test -- --run
#   npm run build
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: uv not found on PATH (needed to run the test suite)" >&2
  exit 1
fi

uv run --no-project --with pytest,pyyaml python -m pytest tests/ -q
