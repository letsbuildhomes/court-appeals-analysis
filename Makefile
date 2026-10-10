UV_CACHE_DIR ?= .uv-cache
export UV_CACHE_DIR
LIMIT ?= 5
TEXT_ARGS ?=
QA_ARGS ?=
REVIEW_PORT ?= 8766
CLAIMS_PORT ?= 8767
APPEALS_ARGS ?=
APPEALS_PORT ?= 8769
DASHBOARD_PORT ?= 8771
DASHBOARD_ARGS ?=
# This checkout lives on a remote server; listen on the private network.
LISTEN_HOST ?= 0.0.0.0
TEXT_IMAGE ?= data-court-review-text:local
TEXT_IMAGE_STAMP = .docker-text-image.stamp
TEXT_DOCKER = docker run --rm --user "$(shell id -u):$(shell id -g)" \
	--network none \
	--mount type=bind,source="$(CURDIR)",target=/workspace,readonly \
	--mount type=bind,source="$(CURDIR)/data",target=/workspace/data \
	--mount type=bind,source="$(CURDIR)/data/archive.duckdb",target=/workspace/data/archive.duckdb,readonly \
	$(TEXT_IMAGE)

.PHONY: all test extract extract-all refresh-listings verify text-image text-test text-inventory text-native text-triage text-all text-ocr text-select text-audit text-finish text-complete text-status text-check text-qa-screen text-qa-compare text-qa-recompare text-qa-status text-qa review claims serve group-build group-status

all: extract

# Offline suite: mocked transports only, no credentials or model calls.
test:
	uv run --inexact python -m unittest discover -s tests

extract:
	uv run python extract.py --limit $(LIMIT)

extract-all:
	uv run python extract.py --all

refresh-listings:
	uv run python extract.py --refresh-listings --limit $(LIMIT)

verify:
	uv run python extract.py --verify-only

$(TEXT_IMAGE_STAMP): Dockerfile pyproject.toml uv.lock
	docker build -t $(TEXT_IMAGE) -f Dockerfile .
	@touch $(TEXT_IMAGE_STAMP)

text-image: $(TEXT_IMAGE_STAMP)
	@docker image inspect $(TEXT_IMAGE) >/dev/null 2>&1 || { \
		docker build -t $(TEXT_IMAGE) -f Dockerfile . && touch $(TEXT_IMAGE_STAMP); \
	}

text-test: text-image
	$(TEXT_DOCKER) python -m unittest discover -s tests -v

text-inventory: text-image
	$(TEXT_DOCKER) python text_pipeline.py inventory $(TEXT_ARGS)

text-native: text-image
	$(TEXT_DOCKER) python text_pipeline.py native $(TEXT_ARGS)

text-triage: text-image
	$(TEXT_DOCKER) python text_pipeline.py triage $(TEXT_ARGS)

text-all: text-image
	$(TEXT_DOCKER) python text_pipeline.py all $(TEXT_ARGS)

text-ocr: text-image
	$(TEXT_DOCKER) python text_finish.py ocr $(TEXT_ARGS)

text-select: text-image
	$(TEXT_DOCKER) python text_finish.py select $(TEXT_ARGS)

text-audit: text-image
	$(TEXT_DOCKER) python text_finish.py audit $(TEXT_ARGS)

text-finish: text-image
	$(TEXT_DOCKER) python text_finish.py all $(TEXT_ARGS)

text-complete: text-all
	$(TEXT_DOCKER) python text_finish.py all $(TEXT_ARGS)

text-status: text-image
	$(TEXT_DOCKER) python text_finish.py status

text-check: text-image
	$(TEXT_DOCKER) python text_check.py

# Exploratory native-text QA. Results stay in data/triage_qa.duckdb.
text-qa-screen: text-image
	$(TEXT_DOCKER) python qa_triage.py screen $(QA_ARGS)

text-qa-compare: text-image
	$(TEXT_DOCKER) python qa_triage.py compare $(QA_ARGS)

text-qa-recompare: text-image
	$(TEXT_DOCKER) python qa_triage.py recompare $(QA_ARGS)

text-qa-status: text-image
	$(TEXT_DOCKER) python qa_triage.py status

text-qa: text-image
	$(TEXT_DOCKER) python qa_triage.py all $(QA_ARGS)

review: text-image
	docker run --rm --user "$(shell id -u):$(shell id -g)" --read-only --tmpfs /tmp \
		--mount type=bind,source="$(CURDIR)",target=/workspace,readonly \
		--publish "$(LISTEN_HOST):$(REVIEW_PORT):8766" $(TEXT_IMAGE) \
		python review_server.py --host 0.0.0.0 --port 8766

claims: text-image
	docker run --rm --user "$(shell id -u):$(shell id -g)" --read-only --tmpfs /tmp \
		--mount type=bind,source="$(CURDIR)",target=/workspace,readonly \
		--publish "$(LISTEN_HOST):$(CLAIMS_PORT):8767" $(TEXT_IMAGE) \
		python claims_server.py --host 0.0.0.0 --port 8767

serve:
	python3 -m http.server --bind $(LISTEN_HOST) 8765

# Build the upstream docket groups.
group-build:
	uv run python groups.py build

group-status:
	uv run python groups.py status

# Supported municipal housing appeal pipeline.
.PHONY: appeals-plan appeals-run appeals-export appeals-status appeals-view appeals-test
appeals-plan:
	uv run python appeals.py plan $(APPEALS_ARGS)
appeals-run:
	uv run python appeals.py run $(APPEALS_ARGS)
appeals-export:
	uv run python appeals.py export $(APPEALS_ARGS)
appeals-status:
	uv run python appeals.py status $(APPEALS_ARGS)
appeals-view:
	uv run python appeals.py serve --host $(LISTEN_HOST) --port $(APPEALS_PORT)
appeals-test:
	uv run python -m unittest tests.test_appeals tests.test_appeal_viewer tests.test_appeal_stats -v

# Read-only experiment inspection; no extraction or model calls.
.PHONY: experiments-view experiments-test
experiments-view:
	python3 appeal_dashboard.py --host $(LISTEN_HOST) --port $(DASHBOARD_PORT) $(DASHBOARD_ARGS)
experiments-test:
	uv run --inexact python -m unittest tests.test_appeal_dashboard tests.test_appeal_impact tests.test_appeal_runner tests.test_appeal_experiments -v

# Targeted experiments on frozen cohorts. Only experiments-run reads .env and
# makes paid model calls; set its cap with EXPERIMENT_ARGS='--max-spend 1.00'.
EXPERIMENT ?=
EXPERIMENT_ARGS ?=
COHORT_ARGS ?=
.PHONY: experiments-cohort experiments-plan experiments-status experiments-run experiments-finalize experiments-compare experiment-name
experiments-cohort:
	uv run --inexact python appeal_cohorts.py select $(COHORT_ARGS)
experiment-name:
	@test -n "$(EXPERIMENT)" || { echo 'Set EXPERIMENT to an experiment name'; exit 2; }
experiments-plan: experiment-name
	uv run --inexact python appeal_runner.py plan $(EXPERIMENT) $(EXPERIMENT_ARGS)
experiments-status: experiment-name
	uv run --inexact python appeal_runner.py status $(EXPERIMENT)
experiments-run: experiment-name
	uv run --inexact --env-file .env python appeal_runner.py run $(EXPERIMENT) $(EXPERIMENT_ARGS)
experiments-finalize: experiment-name
	uv run --inexact python appeal_runner.py finalize $(EXPERIMENT)
experiments-compare: experiment-name
	uv run --inexact python appeal_runner.py compare $(EXPERIMENT) $(EXPERIMENT_ARGS)

# Local research snapshot selection is explicit; MCP runs without pipeline/API credentials.
MCP_RUN ?=
MCP_SNAPSHOT ?= data/research/$(MCP_RUN)
MCP_ARGS ?=
.PHONY: research-setup research-list research-build research-describe research-query research-serve research-test
research-setup:
	uv sync --inexact --extra research-mcp
research-list:
	uv run --inexact python research_mcp.py list $(MCP_ARGS)
research-build:
	@test -n "$(MCP_RUN)" || { echo 'Set MCP_RUN to an explicitly selected run'; exit 2; }
	uv run --inexact python research_mcp.py build --run "$(MCP_RUN)" --output "$(MCP_SNAPSHOT)" $(MCP_ARGS)
research-describe:
	uv run --inexact python research_mcp.py describe --snapshot "$(MCP_SNAPSHOT)"
research-query:
	uv run --inexact python research_mcp.py query --snapshot "$(MCP_SNAPSHOT)" $(MCP_ARGS)
research-serve:
	uv run --inexact --extra research-mcp python research_mcp.py serve --snapshot "$(MCP_SNAPSHOT)" $(MCP_ARGS)
research-test:
	uv run --inexact --extra research-mcp python -m unittest tests.test_research_mcp tests.test_research_cloud -v
