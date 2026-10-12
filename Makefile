# Shortcuts for python -m court; see docs/ARCHITECTURE.md and `python -m court --help`.
# Only targets ending in -send read .env and may make paid model calls; each needs MAX_SPEND.
UV_CACHE_DIR ?= .uv-cache
export UV_CACHE_DIR
COURT = uv run --inexact python -m court
SEND = uv run --inexact --env-file .env python -m court
# This checkout lives on a remote server; tools listen on the private network.
LISTEN_HOST ?= 0.0.0.0
IMAGE ?= court-appeals:local

RUN ?=
PIPELINE ?= production
ARGS ?=
EXPERIMENT ?=
ROUND ?= probe
MAX_SPEND ?=
RESULTS ?=
SNAPSHOT ?=

.PHONY: help test test-all image docker import-september run run-send runs pipelines tags datasets \
	lab-list lab-plan lab-run lab-run-send lab-adopt web grouping-report text-audit \
	research-setup research-build research-describe research-query research-serve research-test

help:
	@$(COURT) --help

# Offline suite: no credentials, no network, no model calls.
test:
	uv run --inexact python -m unittest discover -s tests

# Also replays all 1,395 September cases from the call cache (about 20 seconds).
test-all:
	REPLAY_ALL=1 uv run --inexact python -m unittest discover -s tests

# The whole system with Poppler and Tesseract; run any command inside it:
#   make docker ARGS='run september production'
# Rootless engines (Podman) map the container's root to you; others run as your user.
# Files the container writes in the repository are then yours either way.
DOCKER_USER = $(if $(shell docker info 2>/dev/null | grep -i rootless),,--user "$$(id -u):$$(id -g)")
image:
	docker build --load -t $(IMAGE) .
docker: image
	docker run --rm --pids-limit=-1 $(DOCKER_USER) --mount type=bind,source="$(CURDIR)",target=/workspace \
		--publish "$(LISTEN_HOST):8771:8771" $(IMAGE) python -m court $(ARGS)

# Wrap the September 2026 data as datasets and seed the caches (once; changes nothing it reads).
import-september:
	$(COURT) import-september

# Runs: make run RUN=name PIPELINE=production ARGS='--at extract --input cases=september-cases'
run:
	@test -n "$(RUN)" || { echo 'Set RUN to a run name'; exit 2; }
	$(COURT) run $(RUN) $(PIPELINE) $(ARGS)
run-send:
	@test -n "$(RUN)" -a -n "$(MAX_SPEND)" || { echo 'Set RUN and MAX_SPEND (USD)'; exit 2; }
	$(SEND) run $(RUN) $(PIPELINE) --send --max-spend $(MAX_SPEND) $(ARGS)
runs:
	$(COURT) runs
pipelines:
	$(COURT) pipelines
tags:
	$(COURT) tags
datasets:
	$(COURT) datasets

# Experiments: create one with `python -m court lab new` (docs/LAB.md).
lab-list:
	$(COURT) lab list
lab-plan:
	@test -n "$(EXPERIMENT)" || { echo 'Set EXPERIMENT'; exit 2; }
	$(COURT) lab plan $(EXPERIMENT) --round $(ROUND)
lab-run:
	@test -n "$(EXPERIMENT)" || { echo 'Set EXPERIMENT'; exit 2; }
	$(COURT) lab run $(EXPERIMENT) --round $(ROUND) $(ARGS)
lab-run-send:
	@test -n "$(EXPERIMENT)" -a -n "$(MAX_SPEND)" || { echo 'Set EXPERIMENT and MAX_SPEND (USD)'; exit 2; }
	$(SEND) lab run $(EXPERIMENT) --round $(ROUND) --send --max-spend $(MAX_SPEND) $(ARGS)
lab-adopt:
	@test -n "$(EXPERIMENT)" || { echo 'Set EXPERIMENT'; exit 2; }
	$(COURT) lab adopt $(EXPERIMENT) $(ARGS)

# The web app: live runs, the lab, data viewers and pipelines (read-only).
# Its password comes from COURT_WEB_PASSWORD or .web-password (docs/WEB.md).
web:
	$(COURT) web --host $(LISTEN_HOST)
grouping-report:
	$(COURT) report grouping $(or $(CASES),september-cases)
text-audit:
	$(COURT) report text-audit $(or $(PAGES),september-pages)

# Research snapshots and the MCP server; no pipeline data or credentials reach the server.
research-setup:
	uv sync --inexact --extra research-mcp
research-build:
	@test -n "$(RESULTS)" || { echo 'Set RESULTS to a results dataset'; exit 2; }
	$(COURT) research build --results $(RESULTS) $(ARGS)
research-describe:
	$(COURT) research describe --snapshot $(SNAPSHOT)
research-query:
	$(COURT) research query --snapshot $(SNAPSHOT) $(ARGS)
research-serve:
	uv run --inexact --extra research-mcp python -m court research serve --snapshot $(SNAPSHOT) $(ARGS)
research-test:
	uv run --inexact --extra research-mcp python -m unittest tests.test_research -v
