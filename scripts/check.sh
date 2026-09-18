#!/usr/bin/env bash
# Presubmit: run before pushing.
#
# Checks formatting, linting, and the full test suite, cheapest first so it
# fails fast. The test suite's container-backed tests need Docker with the
# gVisor runtime registered as `runsc`; without it they skip themselves, and
# only the pure-logic tests run.
#
# To fix what the check reports:
#   uv run ruff format .          # reformat
#   uv run ruff check --fix .     # autofix lint
set -euo pipefail

cd "$(dirname "$0")/.."

echo "== ruff format =="
uv run ruff format --check .

echo "== ruff check =="
uv run ruff check .

echo "== pytest =="
uv run pytest

echo "== all checks passed =="
