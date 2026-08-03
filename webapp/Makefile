PYTHON ?= .venv/bin/python
V2 := $(PYTHON) scripts/shareholder_v2.py

.PHONY: test test-python test-frontend check migrate-dry-run compute dispatch worker publisher status codex-smoke-test deploy-dry-run

test: test-python test-frontend

test-python:
	$(PYTHON) -m pytest -q

test-frontend:
	node --test frontend-tests/*.test.mjs
	node --check public/app.js

check:
	$(PYTHON) -m compileall -q app collector liberty_v2 scripts/shareholder_v2.py
	$(V2) health-check

migrate-dry-run:
	$(V2) migrate

compute:
	$(V2) compute

dispatch:
	$(V2) dispatch

worker:
	$(V2) worker

publisher:
	$(V2) publisher

status:
	$(V2) status

codex-smoke-test:
	@test -n "$(SNAPSHOT)" || (echo "Usage: make codex-smoke-test SNAPSHOT=/absolute/VALID-company.json" >&2; exit 2)
	$(V2) codex-smoke-test --company-snapshot "$(SNAPSHOT)" --confirm-real-codex

deploy-dry-run:
	./scripts/deploy_ali.sh --dry-run
