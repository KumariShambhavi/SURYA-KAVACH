#!/usr/bin/env bash
# SURYA-KAVACH one-command launcher (macOS / Linux)
set -euo pipefail
cd "$(dirname "$0")/.."

echo "SURYA-KAVACH starting..."

# 1. Convert raw PRADAN zips -> CSVs if none converted yet
if ! ls data/raw/solexs/*.csv >/dev/null 2>&1; then
  echo "Converting raw PRADAN zips (data/raw is empty)..."
  python -m solarflare.cli convert
fi

# 2. First ingest if no DB yet
if [ ! -f data/flare_ops.db ]; then
  echo "Running first ingest..."
  python -m solarflare.cli ingest --force
fi

# 3. Serve
PORT="${PORT:-8000}"
echo "Serving dashboard at http://127.0.0.1:${PORT}"
python -m solarflare.cli serve --host 127.0.0.1 --port "${PORT}"
