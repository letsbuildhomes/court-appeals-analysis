# Vermont Environmental Division: municipal housing appeals

We study municipal housing appeals heard by the Vermont Environmental
Division. The court publishes its decisions as PDFs; this project turns them
into one row of structured research data per case, with a quote from the
source behind each answer.

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): the system: the pipeline,
  the lab and research access.
- [docs/CODE-ARCHITECTURE.md](docs/CODE-ARCHITECTURE.md): how the Python
  project is organized.
- [docs/LAB.md](docs/LAB.md): testing a change to the pipeline.
- [docs/WEB.md](docs/WEB.md): the web app: watching runs, the lab, and viewers for
  every dataset.
- [docs/MCP.md](docs/MCP.md): querying the results through Claude or another
  MCP client.
- [docs/README.md](docs/README.md): every document, current and historical.

## Setup

Install [uv](https://docs.astral.sh/uv/), Poppler and Tesseract, then:

```sh
UV_CACHE_DIR=.uv-cache uv sync --locked
make test
```

Or use the image with everything installed: `make docker ARGS='--help'`.
Every action is `python -m court COMMAND` (`--help` lists them); the
Makefile has shortcuts.

## Where things are

| Path | What |
| --- | --- |
| `court/` | the framework, contracts, lab, reports and research tools |
| `steps/`, `prompts/` | the step and prompt libraries (`family:variant`; never edited once used) |
| `pipelines/` | pipeline definitions, resolved pipelines by ID, the tag log, the model catalog |
| `experiments/` | experiment records |
| `in/` | raw inputs: PDFs by hash and saved listing pages (not in git) |
| `data/` | datasets, runs, the call and work caches, research snapshots (not in git) |

## The legacy data

The legacy pipeline's production run (September 2026) is reproduced exactly
by the `legacy` pipeline from saved inputs and calls. On a fresh checkout with the data
synced (`scripts/sync-pipeline-data.sh`):

```sh
make import-legacy             # wrap it as datasets; seed the caches
make run RUN=legacy PIPELINE=legacy  # documents, pages, cases and results, with no network or model calls
make web                       # http://SERVER:8771/: live runs, the lab, data and pipelines (password: docs/WEB.md)
```

`SERVER` is this machine's private-network address; browser tools listen on
all interfaces and print their URL.
