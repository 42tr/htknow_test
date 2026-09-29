#!/bin/sh
set -e
exec python3 -u /app/e2e_web.py \
  --bind 0.0.0.0 \
  --port "${E2E_WEB_PORT:-8099}" \
  --runs-dir "${E2E_RUNS_DIR:-/data/runs}" \
  --max-concurrent "${E2E_MAX_CONCURRENT:-2}" \
  --max-history "${E2E_MAX_HISTORY:-50}" \
  "$@"
