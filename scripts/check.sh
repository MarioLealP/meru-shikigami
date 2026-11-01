#!/usr/bin/env bash
# Every check CI runs, runnable locally: lint, format, types, the lockfile, pip-audit, tests and the generated schema.
set -euo pipefail
cd "$(dirname "$0")/.."

step() { printf '\n== %s\n' "$*"; }

step ruff
uv run ruff check .
uv run ruff format --check .
step ty
uv run ty check
step "uv.lock up to date"
uv lock --check
step pip-audit
uvx pip-audit --disable-pip --require-hashes --progress-spinner off -r <(uv export --frozen --no-emit-project --quiet)
step pytest
uv run pytest -q
step "schema/ up to date"
fresh=$(mktemp -d)
trap 'rm -rf "$fresh"' EXIT
uv run shikigami-schema "$fresh" >/dev/null
diff -r schema "$fresh"
printf '\nall checks passed\n'
