# Code architecture

Status: implemented, October 12, 2026. This document sits one level below
[ARCHITECTURE.md](ARCHITECTURE.md). It describes how the Python project is
organized and which patterns it follows. It is a direction, not a
specification: names and module boundaries may change as the code is written.
The rules under "Principles" should not.

## Principles

1. **One building block.** Every unit of work is a step. Its inputs and
   output are keyed maps with declared contracts, and how it processes them
   is its own business. A whole pipeline, a stage and a single model call are
   all steps.
2. **Services, not assumptions.** Steps get models, caching, tracing and
   checkpoints from services. The framework never decides how items flow,
   batch or parallelize.
3. **Append-only history.** Step variants and prompts are never edited once
   used. Pipeline definitions, saved datasets and tags are never overwritten.
   Every version that has run stays runnable.
4. **Version what can change underneath.** The step interface, the contracts
   and the dependency lockfile are versioned, so old steps keep working
   without being rerun.
5. **Separate the logic from the I/O.** The interesting work, such as
   grouping rules, quote checking or comparing two results, is written as
   plain functions over plain values. Steps are thin wrappers around them, and
   tests exercise the plain functions directly.
6. **One owner per concern.** Paths and settings live in one place.
   Spending, retries and the call cache live in the models service. Nothing
   else retries, caches calls or spends.
7. **Imports point one way,** and a test checks it.
8. **Tools read; runs write.** Viewers, reports, the dashboard and the MCP
   server never change a dataset.

## Repository layout

```text
court/              the Python package
  core/             step interface, services, runner, library loader, pipeline
                    resolution and tags, dataset storage, the models service
  contracts/        saved-data contracts: documents, pages, case sets, results
  lab/              comparison, experiments, the lab's views
  reports/          reports on saved datasets
  ui/               the web app: its API and static pages
  research/         snapshot builder, MCP server, remote check
  web/              small shared helpers for the read-only web tools
  migrate/          the one-time import of the September 2026 data
  config.py         paths and settings
  cli.py            the single command-line entry point (python -m court)
steps/              the step library; the file for family:variant is family/variant.py
  flow/sequence.py                 flow:sequence, the generic composite
  fetch/vtec-environmental.py      fetch:vtec-environmental
  text/ocr/tesseract.py            text/ocr:tesseract
  review/legacy.py                 review:legacy
  legacy/v6/validation.py          legacy/v6:validation, a helper several steps use
  ...
prompts/            the prompt library, same naming (review/legacy.md is review:legacy)
pipelines/
  definitions/      authored definitions: a base ID plus changes, or a full tree
  resolved/         every resolved tree, stored under its pipeline ID
  tags.jsonl        the tag log
  models.json       model catalog: provider type, price function, credential prefix
experiments/        experiment records
tests/
```

The package is the framework and the tools. `steps/`, `prompts/` and
`pipelines/` are content the framework loads at run time; nothing imports
them as ordinary modules.

**One command.** Every action goes through one command with subcommands, for
example "run", "lab plan", "lab run", "report grouping" or "research build".
Each subcommand is a thin adapter: it parses arguments, then calls one
function. The Makefile becomes a short list of shortcuts plus the Docker
image target.

## Dependency rules

```text
     contracts              what each saved dataset contains; imports nothing
         ▲
       core     ◀── config  interface, services, runner, loader, datasets
     ▲   ▲   ▲
 steps  lab  research, reports
```

- A library entry imports `core`, `contracts` and the standard library or
  locked dependencies. It gets helper entries through the loader, by exact
  name, and never imports the lab, reports, research access or the CLI.
- `lab`, `reports` and `research` import `config`, `core`, `contracts` and
  `web`. They run pipelines through the runner and never load a library entry
  themselves, except to describe a step in the web app.
- `ui`, the web app, is a consumer like the command line: it may also import
  `lab` and `reports`. Nothing imports `ui` but the command line.
- Nothing imports `lab` or `research`.
- One test reads every module's imports and enforces these rules.

## Core

**The step interface.** A step has a name, settings, and optionally a prompt
and children. It declares its input and output contracts, the interface
version it targets, and one run operation: input maps and a context in, an
output map out. Each output entry is a value or a failure with a reason.

**Common steps.** A few generic composites cover the usual patterns: run
children in sequence, apply a child to each item in parallel, and form
batches by a rule. They're configured in pipeline definitions like any other
entry. A step that needs another shape implements its own run operation.

**Library loader.** Finds an entry by its `family:variant` name, checks
that the file still matches the hash recorded when it was first used, and
loads it. Files keep plain paths, `family/variant.py`, because colons in file
names break on macOS and Windows.
A test runs the same hash check over every entry referenced by any resolved
pipeline, which is how "never edited once used" is enforced.

**Pipeline resolution.** Turns a definition into a full tree: start from
the base pipeline's resolved tree, apply the replacements at their slash
paths, and fill in defaults. The resolved tree names every position's
library entry, prompt and settings, plus the hashes of the entries and
prompts and the lockfile hash. Its hash is the pipeline ID. It's saved under
that ID and never changed. Paths and position names come only from the
definition.

**Interface versions.** Core keeps every step interface version that some
resolved pipeline uses. A breaking change adds a new version, and a thin
adapter keeps the old one working. A small test suite per interface version,
run against toy steps, guards each one.

**Context and services.** Every step's context carries the services:
- **Models.** Call cache, spending cap, the single retry policy, dry-run
  estimates, cost records.
- **Cache.** Per-item reuse for expensive code work.
- **Trace.** A per-item record of steps, calls and outcomes.
- **Checkpoints.** Saving a step's output as a dataset.
- **Progress.** Recording each step's start and finish for the live view.

A step reaches the outside world only through these, so tests and
simulations can swap any of them, for example a fake models service.

**Datasets.** Generic keyed storage with the identity rule: keys, content
hashes, a content ID and a manifest of the producer and inputs. Every dataset,
from documents to results, is a directory with a manifest and one JSON file
per item (a key's slashes become subdirectories), which is easy to diff, copy
and read. A generic diff gives identical, changed, added and removed keys for
any two datasets of the same contract.

**Runner.** Runs a pipeline ID on inputs, which may be any saved datasets,
optionally limited to chosen keys. It provides the services, saves the
checkpoints, and records a run manifest: the pipeline ID, the inputs, the
cost and when it ran. While a run is going, its manifest says so, a progress
service rewrites `progress.json` once a second (each step's items, failures
and items touched; each item's last step), and the models service appends
every call's reservation and charge, labeled with its step and tokens, to the
ledger. The web app reads only these files to show a run live.

## Contracts

There's one module per saved dataset kind: documents, pages, case sets and
results. Each defines the item shape, its key and its validation. The results
contract also covers quote verification and the catalog of research facts
used in comparisons. Contracts are versioned like the step interface, and a
change to one is deliberate, because steps and every consumer depend on it.
Contracts for intermediate data inside a subtree live with the steps that use
them, as library entries.

## The step library

Each entry is one file defining one step, plus any helpers only it uses.
Its logic lives in plain functions in that file, so it can be tested
directly. A few notes on today's steps:

- **Fetch.** One source entry per listing, built on a shared downloader entry
  that handles pacing, network retries, verification and storage by hash.
- **Text.** Page-level entries: native extraction, a usability check, OCR
  (cached per page) and selection.
- **Cases.** Grouping is an entry made of small named rules applied in
  order. Rendering case text is another. The first milestone is
  reproducing September's case set with identical hashes.
- **Extract.** Model steps build a request, check the answer and produce a
  value per item. The `legacy` variants reproduce September's extraction,
  and that reproduction is the main regression test. Providers, one per
  API style, belong to the models service in core.

**Prices.** Each model in `pipelines/models.json` names a price function and
its settings. `flat` charges the same rates at every hour; `peak-hours`
multiplies them during listed hours on listed weekdays, which is how
DeepSeek's weekday peak (twice the price, 01:00–04:00 and 06:00–10:00 UTC,
Monday to Friday) is described. A call's worst-case cost is reserved at the
rates when it is sent and charged at those rates, so caps, the ledger and the
live run view all use the provider's actual prices. Another schedule is a
new price function in `court/core/models.py` and an entry in the catalog.

Reports and viewers about these outputs, such as the grouping report, the
case-set diff and the text audit, are tools in the package. They read
datasets and aren't steps.

## Lab

The lab is application code on top of the runner and datasets.

- **Experiments.** A record in `experiments/` holds the hypothesis, the
  pipeline ID it tests, the baseline ID, the probe keys and the results of
  each round. New variants and prompts are added to the libraries; the new
  pipeline is a definition in `pipelines/definitions/`. The lab plans an
  experiment (definition diff and dry-run cost, no model calls), runs the
  probe, then the confirm round, and records each comparison.
- **Adoption.** Appends a tag entry after checking that a confirm round
  passed.
- **Comparison.** A plain function from two results maps, plus an optional
  reference map, to a per-case, per-fact verdict.
- **Data diffs.** Generic dataset diffs at each tree position the two
  pipelines share and both saved or traced.
- **Reference map.** Results storage with one addition: a field can be marked
  "not labeled". Building it is its own small tool. A strong model drafts
  answers case by case and a person checks them.
- **Dashboard.** A read-only web server that renders lab records, resolved
  trees and datasets. It contains no comparison logic of its own.

## Research

A snapshot builder turns one results map, plus its case set for evidence,
into a read-only SQLite database of research facts. The MCP server and the
viewer read only snapshots.

## Testing

- **Unit tests** cover the plain functions in library entries and tools with
  tiny inline inputs, and the core composites against toy steps.
- **Interface tests**, one suite per step interface version.
- **Library integrity:** every entry used by a resolved pipeline still
  matches its recorded hash.
- **One end-to-end test** runs a few synthetic PDFs through a whole pipeline
  with a fake models service and compares the result with a checked-in
  results map. It shows the steps fit together and that dataset IDs are
  deterministic.
- **Regression tests** run against real data when it's present:
  - September's case set must rebuild identically.
  - The `legacy` extraction must reproduce production's results map.
- **The dependency test** enforces the import rules.
- No test reads credentials or calls a paid model.

The fake models service also gives the lab a free practice mode later,
replacing the shelved sandbox work.

## Running

The package installs with its Python dependencies through uv. Poppler and
Tesseract are system dependencies. One Dockerfile builds an image with
everything, and the command can run inside it with the repository and data
mounted. Web tools listen on all interfaces, because the server is reached
over the private network.

## Status

All seven steps are done:

- `core`, `contracts`, and the dependency and library-integrity tests.
- Fetch, text, cases and the September extraction as library entries, in one
  definition (`pipelines/definitions/september.json`), tagged `production`.
- Run end to end from the saved September listing snapshot, text results and
  model calls, it reproduces September's documents, pages and case set as
  identical datasets, and its 1,385 results exactly; the same 10 cases fail
  at review.
- The lab, reports, web app and research access on datasets; the old scripts
  are deleted.
