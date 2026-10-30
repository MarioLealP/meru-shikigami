#!/usr/bin/env bash
# Every check CI runs, runnable locally: lint, format, types, tests, the lockfile and the generated schema.
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
step pytest
uv run pytest -q
step "schema/ up to date"
fresh=$(mktemp -d)
trap 'rm -rf "$fresh"' EXIT
uv run shikigami-schema "$fresh" >/dev/null
diff -r schema "$fresh"
printf '\nall checks passed\n'
