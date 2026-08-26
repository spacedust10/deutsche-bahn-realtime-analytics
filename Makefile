.PHONY: help install up down db collect serve train test test-fast lint clean

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Install Python dependencies
	python3 -m pip install -r requirements.txt

up:  ## Start everything (postgres, collector, dashboard) — Ctrl-C stops it
	sh scripts/up.sh

down:  ## Stop the background collector and the postgres container
	-pkill -f -- "-m dbrt"
	docker compose down

db:  ## Create the database and apply the schema
	createdb $${PGDATABASE:-dbrt} 2>/dev/null || true
	psql -d $${PGDATABASE:-dbrt} -f db/schema.sql

collect:  ## Run the realtime collector (Ctrl-C to stop)
	python3 -m dbrt

collect-once:  ## Run a single poll cycle
	python3 -m dbrt --once

serve:  ## Serve the dashboard on http://127.0.0.1:8000
	python3 scripts/serve.py

train:  ## Train the delay model from collected history
	python3 scripts/train_model.py

test:  ## Run the full test suite
	python3 -m pytest

test-fast:  ## Run only tests that need neither PostgreSQL nor the network
	python3 -m pytest -m "not postgres and not network"

clean:  ## Remove caches and downloaded feed data
	rm -rf .pytest_cache **/__pycache__ data/*.zip
