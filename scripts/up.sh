#!/bin/sh
# The whole stack in one command: postgres, collector, dashboard.
# Ctrl-C stops whatever this script started; anything already running is left alone.
set -e
cd "$(dirname "$0")/.."

if ! pg_isready -q -h "${PGHOST:-localhost}" -p "${PGPORT:-5432}" 2>/dev/null; then
	echo "==> postgres"
	docker compose up -d --wait
else
	echo "==> postgres already up"
fi

# The collector applies the schema itself on startup, so there is no separate db step.
if pgrep -f -- "-m dbrt" >/dev/null 2>&1; then
	echo "==> collector already running"
else
	echo "==> collector (log: data/collector.log)"
	mkdir -p data
	python3 -m dbrt >>data/collector.log 2>&1 &
	collector=$!
	trap 'kill "$collector" 2>/dev/null || true' EXIT INT TERM HUP
fi

echo "==> dashboard  http://${API_HOST:-127.0.0.1}:${API_PORT:-8000}"
python3 scripts/serve.py
