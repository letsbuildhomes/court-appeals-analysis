# Extraction feedback loop: development and migration handoff

Checkpoint date: October 9, 2026. Branch: `feature/extraction-feedback-loop`.
This branch preserves the infrastructure and dashboard work developed after
`e4fad8f` (`added: mcp server`, September 30), plus this handoff and a data-transfer
script. It is a development checkpoint, not a completed extraction experiment or
a new research release. Use `git rev-parse HEAD` to identify this checkpoint's
exact commit; do not attribute it to the original model run.

## Purpose and agreed scope

The first production run brought the archive through extraction, review,
duration, and a structured research MCP snapshot. The next goal is a dependable
feedback loop: ask a quality question, freeze a hypothesis and cohort, run
targeted variants on the same inputs, inspect changes against source evidence,
and promote changes only when their benefits and regressions are understood.

The implementation should allow different extraction strategies. Today's
triage/extract/review/duration topology is one producer, not the definition of a
research record. The experiment boundary is frozen, page-addressable extracted
text. Leave ingestion and OCR alone for this phase. Preserve the original run,
its sources, prompts, calls, costs, and outputs as the historical baseline.

The completed infrastructure scope explicitly deferred production reference
selection, alternative extraction strategies, paid model calls, run launching,
and live experiment orchestration. Synthetic examples exercise the software;
their correctness scores do not measure production extraction quality.

## Implemented in this branch

| Component | Current behavior |
| --- | --- |
| `appeal_assembly.py` | Shared checkpoint/status interpretation and finalized-record assembly, used by the runner, viewer, and research projection. |
| `appeal_contracts.py` | Read-only adapter from saved `appeals-1` runs to `appeal-cases-1`, including source/prompt verification and separate scope and execution status. |
| `appeal_experiments.py` | Creates immutable historical-baseline experiment artifacts and validates their digests and identities on read. Executes no model work. |
| `appeal_reference.py` | Versioned reference expectations, evidence, comparison rules, adjudication state, and completeness. |
| `appeal_evaluation.py` | Paired field comparisons, record matching, corrections/regressions, missing/additional records, source incompatibility, and explicit unscored states. |
| `appeal_demo.py` and evaluation fixtures | Invented baseline/candidate/reference examples with known differences. |
| `appeal_dashboard.py`, `dashboard.html`, `dashboard.js`, `dashboard.css` | Read-only Review, Pipeline, Inputs, and Runs & costs views; case drilldowns, verified source evidence, prompt/configuration inspection, and cost provenance. |
| `appeal_controls.js` | Shared styled dropdowns, including dynamically rendered controls and keyboard interaction. |

For example:

```python
from appeal_experiments import read_experiment
from appeal_evaluation import evaluate

baseline = read_experiment('/path/to/baseline')
candidate = read_experiment('/path/to/candidate')
comparison = evaluate(baseline, candidate, reference=None)
```

This reports output movement without assigning correctness to unreviewed
changes. Only finalized records count as research output. Draft candidates,
review responses, and failed execution remain available as diagnostics.

Case identity is run-local. Cross-run comparisons require matching frozen-source
hashes as well as case IDs. Record matching uses docket identity within a case;
ambiguous or missing identities need explicit treatment. Pending, disputed,
unknown, or absent labels are unscored. An adjudicated `unknown` answer can be a
valid expected value. Accuracy and comparison coverage have separate meanings.

The public MCP retains its curated structured SQLite projection. Canonical
experiment artifacts and the internal dashboard can contain source prose and
evidence and are not automatically published through that interface.

Detailed contracts and operating instructions:

- [Frozen-text boundary](../PIPELINE_CONTRACT.md)
- [Evaluation framework](evaluation-framework.md)
- [Reference selection proposal](reference-selection-proposal.md)
- [Experiment dashboard](EXPERIMENT-DASHBOARD.md)
- [Supported extraction pipeline](APPEALS.md)
- [Research approach](APPEALS-RESEARCH.md)

## Baseline and local data layout

The imported production archive lives at:

```text
data/production-import-UazDoN/
  in/                         # imported production PDFs and listings
  data/
    appeals/full-run/         # frozen prompts, sources, calls, checkpoints, exports
    research/shared-v1/       # published manifest.json and facts.sqlite
    ...                       # imported supporting databases and artifacts
data/experiments/production-baseline/
  manifest.json
  cases.json
```

The top-level `in/` and `data/` also contain the laptop's earlier archive,
supporting databases, and local test/release/sanity runs. Those runs are
`final-smoke-20260929-190157`, `release-check`, `sanity-two-group-20260930`, and
`sanity-two-group-live-20260930`. Treat the nested imported `full-run` as the
production baseline. Do not silently merge it with top-level test data or assume
the default runner/viewer paths select it. Ordinary new planning reads the
top-level grouping/text databases; planning from frozen production inputs still
needs deliberate support or an explicitly established production workspace.

The imported baseline and experiment artifact currently agree on:

| Measure | Count |
| --- | ---: |
| Planned source groups | 1,395 |
| Complete groups, including exclusions | 1,380 |
| Review failures | 10 |
| Scope flags | 5 |
| Finalized docket records | 513 |
| Excluded groups | 953 |
| Confirmed-scope groups | 327 |
| Uncertain-scope groups | 115 |

Group counts and docket-record counts are different denominators. The completed
run exports 382 confirmed-scope and 131 uncertain-scope docket records. These
figures describe the preserved import, not a freshly checked live server. The
user supplied `e4fad8f` as the remote checkout revision at import; that alone
does not establish the code revision of every historical model call.

The 10 saved review failures are six incomplete model responses, two failures to
cover every batch member exactly once, one invalid changes/flags structure, and
one citation to a nonexistent page (`g-209-11-04-vtec`). The five flagged groups
are `g-106-6-03-vtec`, `g-195-9-07-vtec`, `g-238-11-05-vtec`,
`g-25-env-00010`, and `g-73-2-02-vtec`. Two involve potentially upgrading unclear
housing scope; three involve inappropriate docket inclusion or enforcement/
home-business proceedings. Inspect saved source evidence before resolving them.

Earlier resume conflicts were fixed in pushed commit `ace0256`: completed
neighbors survive retries, raw reviews remain unchanged during validation, and
duration call identities include request hashes. The historical 41-issue report
is superseded by this import's 15 outstanding groups. An identical retry is not
assurance that output-limit failures will recover.

## Open questions and next development work

1. **Review interface.** The first dashboard redesign was implemented and
   visually checked. The last discussion proposed replacing the large Regressed,
   Improved, and Changed boxes with a case-focused queue. Those boxes count
   fields while the table lists cases, which was confusing. Proposed columns:
   `Case | What changed | Assessment`, with compact case-count filters for all
   cases, known regressions, other changes, and comparison issues. Opening a case
   should explain why it needs attention, show before/after values and reviewed
   expectations, then show evidence. This latest proposal remains unimplemented.
2. **First real experiment.** Choose a narrow question and save both its selection
   query and resulting IDs. Review failures are one pilot opportunity; unknown
   durations are another. An unknown duration may be correct. Preserve batch
   companions when testing the current paired-review strategy, or explicitly
   record batching as a changed variable.
3. **Reference selection and adjudication.** Review the methodology proposal.
   Decide the eligible population, representative and challenge samples, pilot
   size, field rubric, reviewer/adjudicator process, related-proceeding clusters,
   development/held-out split, and spending cap. Earlier suggestions of five or
   30–50 cases were exploratory, not an approved sample. Sample size should follow
   pilot evidence. An Astra-produced record is a candidate reference until
   independently reviewed and adjudicated.
4. **Execution interface.** Implement controlled execution from frozen cases,
   reuse of eligible upstream artifacts, and a candidate producer. Currently
   `appeal_experiments.py` only imports completed/saved work; it is not a run
   orchestrator. Live progress, run launch, and reference-editing controls are
   future capabilities.
5. **Promotion and release selection.** Define how accepted targeted results
   compose a complete release. The research snapshot builder selects a single
   run today; automatically choosing the latest planned run is not an adequate
   release policy. Record source hashes, code revision, prompt/config/model
   identity, selected cases, cost, and explicit result selection.
6. **Remaining failures and scope.** Determine recovery strategies and adjudicate
   the five scope flags. Keep strict evidence checks and successful checkpoints.
   Do not delete saved requests, restore an older run, or automatically treat a
   scope flag as an ordinary retry.
7. **Broader MCP audience.** The live experiment was reported working. Wider
   access remains a separate decision: isolation, bounded queues, rate/resource
   limits, access policy, dependency maintenance, and adversarial/load tests were
   proposed. Also decide whether every exposed field is suitable for bulk
   download and test clients' treatment of joins, unknowns, and unsupported
   causal/impact claims. This branch does not implement that hardening pass.
8. **Shared validation cleanup.** Centralized assembly is implemented, but the
   supported pipeline still relies on validation and helpers in historical
   modules such as `group_scout.py`. Extracting those helpers into focused modules
   remains possible future work; do not remove dependencies merely because their
   names refer to earlier research.

Useful prior Codex conversations (titles are the app's actual titles):

| Title | Thread ID | Handoff relevance |
| --- | --- | --- |
| Design extraction feedback loop | `01a0f479-9bee-75d2-abcd-6210888c9fe9` | Scope decisions, baseline import, implementation, UX refinement, and last case-table proposal. |
| Assess MCP data exploration | `01a0f315-880d-7e80-bbb7-553e730a3768` | Working deployment and discussion of wider access. |
| Investigate pipeline phase stop | `01a0f042-5897-72b0-b0c8-dc2ccf624c5d` | Failure diagnosis, resume fix, and reasoning/output-limit concern. |
| Review extraction pipeline handoff | `01a0ea6d-e2d5-7220-a4b3-a9c4652c4c88` | Previous server operation and dashboard updates. |
| Use Codex with remote workstation | `01a0d1a5-dc27-77d3-9438-6a5680e73413` | Earlier remote-development discussion. |
| Audit Unfinished Work | `01a1128a-2bb1-7f50-9b41-824ed83d7ee1` | October audit and this migration preparation. |

The document captures the decisions needed to continue even if the new host does
not have the laptop's Codex conversation history. Read the current tools/docs
before following old product setup advice or obsolete shell commands.

## Move code and data

The data-sync script copies all three saved-work directories, including local
history, nested production data, experiment artifacts, exports, and generated
reports. Approximate sizes at this checkpoint: `in/` 463 MB, `data/` 1.7 GB,
`out/` 7.9 MB. Allow additional destination space for dependencies, images, and
future work. No extraction or paid API call is needed to transfer them.

First make the committed branch available to the destination. From the laptop:

```sh
git push -u origin feature/extraction-feedback-loop
git rev-parse HEAD
```

On the new host, choose a fresh checkout directory and check out the same commit:

```sh
git clone --branch feature/extraction-feedback-loop \
  git@github.com:letsbuildhomes/court-appeals-analysis.git \
  /home/USER/src/court-appeals-analysis
cd /home/USER/src/court-appeals-analysis
git rev-parse HEAD
```

Replace the host/user/path placeholders. An SSH config alias can supply a port,
key, or jump host. The script requires `git`, `ssh`, `rsync`, and `python3` locally,
and `git`, `rsync`, and `python3` remotely. It supports the laptop's built-in
legacy rsync and ordinary Linux rsync. Use an absolute destination path containing
letters, digits, slashes, underscores, dots, and hyphens; spaces are deliberately
unsupported. The remote checkout must already exist.

From this repository on the laptop, preview:

```sh
bash scripts/sync-pipeline-data.sh USER@HOST /home/USER/src/court-appeals-analysis
```

Stop ingestion, OCR, extraction, experiment execution, and any other data writers
on both machines. Then transfer:

```sh
bash scripts/sync-pipeline-data.sh --apply --writers-stopped \
  USER@HOST /home/USER/src/court-appeals-analysis
```

The script checks matching Git commits, requires clean source code, refuses
nonempty destination data directories on the first apply, and refuses top-level
data symlinks. It uses resumable rsync without deletion and checks file contents
by checksum. It captures and restores file modification times at nanosecond
precision because readers use those times to choose checkpoints; legacy macOS
rsync alone truncates that precision. The remote filesystem must support it.

If interrupted, keep writers stopped and repeat with `--resume` as well as
`--apply --writers-stopped`. Use `--resume` only for the same migration: it permits
replacing matching destination paths. Destination-only files are retained, so
checksum verification establishes that every source file arrived, not that an
arbitrary preexisting destination is an exact copy. After switching development
to the destination, do not resume syncing an older laptop copy over it.

## Restore the environment and relocate evidence

Copy `.env` separately if its credentials remain appropriate, using the same
SSH destination. For example, run from the laptop:

```sh
scp .env USER@HOST:/home/USER/src/court-appeals-analysis/.env
ssh USER@HOST 'chmod 600 /home/USER/src/court-appeals-analysis/.env'
```

It is now Git-ignored and the data script excludes it. Variable names are
`APPEALS_API_BASE_URL`, `APPEALS_API_KEY`, and `APPEALS_MODEL`. Credential values
are not recorded in this document. Load the environment file only when running
commands that need it; inspection and offline tests need no API credentials.

Rebuild dependencies and images on the new host rather than copying macOS
environments or Docker's cache:

```sh
UV_CACHE_DIR=.uv-cache uv sync --locked --extra research-mcp
.venv/bin/python -m unittest discover -s tests -v
```

Use the project's Python range (3.12–3.13) and install Docker when text/OCR or
containerized viewers are needed. `make text-image` rebuilds the text image;
`.docker-text-image.stamp` is excluded from transfer. The research image now
includes `appeal_assembly.py` in both its COPY list and allowlisted context;
the transfer instructions in `MCP-CLOUD.md` have been updated accordingly.

The original experiment manifest's `source_run` is an absolute laptop path.
Copying its bytes preserves history but does not relocate its evidence reader.
On the destination, create a *new* adapter artifact from the transferred source
run, preserving the original artifact:

```sh
.venv/bin/python appeal_experiments.py import-run \
  "$PWD/data/production-import-UazDoN/data/appeals/full-run" \
  "$PWD/data/experiments/production-baseline-relocated" \
  --code-revision "$(git rev-parse HEAD)"
.venv/bin/python appeal_experiments.py inspect \
  data/experiments/production-baseline-relocated
.venv/bin/python appeal_dashboard.py \
  --baseline data/experiments/production-baseline-relocated
```

Select **Supplied experiment artifacts** in the dashboard. Its default remains
the synthetic fixture, and the separate default production-baseline entry still
points to the original manifest. The new artifact verifies frozen sources and
prompts without OCR or model calls. `--code-revision` describes this adapter
invocation, not the historical extraction's code. Do not edit the old baseline
in place just to hide its laptop path.

The dashboard binds to loopback and validates local hosts/origins. From the
laptop, an SSH tunnel preserves that model:

```sh
ssh -N -L 8771:127.0.0.1:8771 USER@HOST
```

Open `http://127.0.0.1:8771/dashboard`. Use a different local port if occupied.
Validate source evidence on a real imported case, not just the synthetic demo.
The published research snapshot pair is already portable; use its documented
inspection/smoke commands with an explicitly selected snapshot.

## Before retiring the laptop directory

- Confirm sync checksum verification, nanosecond timestamp restoration, matching
  code revision, and destination tests. Inspect the production baseline and at
  least one verified source; confirm 1,395 groups and 513 finalized records.
- Compare the import with any still-existing `server-a` production run. That
  server may contain work done after this snapshot. Decide which copy is
  authoritative before restarting production development.
- Resolve production working paths before new planning/resume. The preserved
  production run remains nested; the default `data/appeals` contains test runs.
- Transfer/configure `.env`, SSH access, Git authentication, and model-provider
  access. These are machine/account configuration, not data-directory contents.
- Establish process startup, dashboard tunnels or hosting, backups, and the
  authoritative Git/data location. Existing server/proxy configuration lives
  outside this repository and is not transferred by this script.
- Stop remaining laptop services and redirect shortcuts or browser bookmarks
  once the destination is working. Retain the laptop data or another verified
  backup until acceptance is complete.
- `.venv/` (~359 MB), `.uv-cache/` (~374 MB), `__pycache__/`, Python bytecode,
  `.docker-text-image.stamp`, and Docker images are rebuildable. They need no
  manual content migration. Git history moves through clone/fetch; Codex history,
  browser state, and temporary screenshots outside the repository do not move
  with the data script. This handoff captures the active design context.

The new destination becomes the working environment only after these checks.
The script performs a copy and does not retire or delete the laptop checkout.

## Validation record

The prior implementation thread recorded 175 tests with one skipped and no
failures, browser checks for comparison/evidence/keyboard flows, and an unchanged
production research projection after shared assembly refactoring. Migration
checks additionally exercise preview, same-commit enforcement, clean-source
checks, stopped-writer acknowledgement, nonempty-destination refusal, resumed
transfer, source-only credential exclusion, nanosecond timestamps, and repair of
same-size/same-time corruption with real local rsync and an SSH stand-in. The
MCP packaging regression imports the Docker COPY contents in an isolated folder.
At this checkpoint, the suite ran 181 tests with one Tesseract-dependent test
skipped. Two loopback HTTP tests initially encountered sandbox binding
restrictions; both passed when rerun outside the sandbox. All other tests passed.
Shell syntax, JavaScript syntax, and Git whitespace checks passed. The existing
baseline artifact was validated and all 1,395 source texts passed the dashboard's
SHA-256 evidence verification; its 513 finalized records remain available.
No live destination transfer is claimed until an actual host/path is supplied
and the script is run there.
