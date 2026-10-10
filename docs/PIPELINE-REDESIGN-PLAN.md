# Pipeline redesign plan: steps as code, one result per case

Status: agreed design, not yet implemented. Written October 10, 2026 at the end
of a long design thread, for a fresh agent to build from. Read this whole file,
then `AGENTS.md`, `docs/EXPERIMENTS.md` and `docs/PIPELINE.md` (which describes
the interim recipe design this plan replaces).

## Hard constraints

- **Never make paid model calls.** Do not load `.env` or run anything that
  could reach the model provider. Verify by comparing the *inputs* to model
  steps (requests) with saved ones, using fake transports or sending disabled
  and a zero spending cap. Running the real pipeline costs money; matching
  inputs is sufficient evidence.
- **Repository rules** (`AGENTS.md`, user global instructions): answer
  questions in chat; change files or run pipelines only when directly asked;
  commit only when asked, on the checked-out branch; push only when asked.
  Commit messages follow the house style (`added:`/`changed:`/`fixed:`/
  `refactor:`/`removed:`, lowercase past-tense subject, retrospective body).
- **Server environment:** this checkout is on a remote server. Browser tools
  listen on all interfaces (`LISTEN_HOST`, default `0.0.0.0`); share URLs with
  the LAN address (currently `192.168.88.3`). The experiment dashboard runs on
  port 8771 (`make experiments-view`). Docker commands go to rootless Podman
  only if `DOCKER_HOST` is set; agent shells may not have it.
- **Preserve data:** never modify `data/production-import-UazDoN/` (the
  production run) or existing artifacts in `data/experiments/`. New work writes
  new directories.
- **Tests:** `make test` runs the offline suite (no credentials); keep it green.
  Seven text-pipeline tests skip without Poppler; that is expected here.

## Background

The project extracts structured research records about municipal housing
appeals from Vermont Environmental Division court PDFs. Text extraction/OCR is
frozen; the experiment boundary is frozen, page-addressable source text per
case ("group"). The production run (`full-run`, 1,395 groups) has 513 finalized
docket records; 953 groups excluded, 327 confirmed, 115 uncertain scope; 10
review failures and 5 scope flags (details in `docs/EXPERIMENTS.md`).

Already on `main` (commit `62d740a`): cohorts (`appeal_cohorts.py`), canonical
artifacts (`appeal_contracts.py`, `appeal_experiments.py`), fact-level impact
measurement (`appeal_impact.py`: about 20 research facts, verdicts improved /
worsened / mixed / needs judgment / unchanged, unverified without reference
labels), reference scoring (`appeal_evaluation.py`, `appeal_reference.py`), the
experiment dashboard (`appeal_dashboard.py`, `dashboard.*`), and an earlier
experiment runner. Two retrospective same-prompt comparisons live in
`data/experiments/` (`same-prompt-rerun`, `smoke-replication`); they showed
4–11% of facts change between identical-prompt runs, so experiment effects must
exceed that noise.

The production model is `deepseek-flash` through an OpenAI-compatible chat API
(`APPEALS_API_BASE_URL`, `APPEALS_API_KEY`, `APPEALS_MODEL` in `.env`). Prices:
$0.15/M input, $0.003/M cached input, $0.60/M output.

## Current branch status

Branch `feature/pipeline-recipes` (local only, uncommitted, based on `main`
`62d740a`) holds an **interim** design: a central `recipes/appeals/recipe.json`
with providers, prompts, steps and a fixed per-case-then-batched phase order,
run by `pipeline/engine.py`. The user rejected that shape as too prescriptive.
Its parts are useful raw material:

| Keep (adapt) | Replace |
| --- | --- |
| `pipeline/providers.py`, `pipeline/models.json`: provider interface (chat and stateless decision/typed-question kinds), per-model pricing, `bound`/`estimate`/`price` | `recipes/appeals/recipe.json`, `pipeline/recipe.py` (central config, composition, two-phase validation) |
| `appeal_runtime.Client`: spending cap with pre-call reservation, saved calls, retries, JSON repair, read-through `inherit`, `dry_run` ledger | `pipeline/engine.py` (two-phase control flow, checkpoint layout) |
| `pipeline/conversation.py`: immutable conversation value | `pipeline/components/base.py` interface (case vs batch scope) |
| `pipeline/batching.py`: `sequential` and `single` groupers (become selector helpers) | Call naming by step/batch names (becomes content addressing) |
| `pipeline/components/{triage,extract,review,duration}.py`: exact request-building logic of the legacy pipeline (port into steps) | `appeal_runner.py` definition-directory composition (becomes module-based) |
| `tests/test_pipeline_replay.py` idea: rebuild production requests with sending disabled | Dashboard "What changed" flat phase graph (becomes a tree) |
| `appeal_runner.py` flow: plan with dry-run estimate, capped run, finalize, retrospective compare, cohort companions | `docs/PIPELINE.md`, `docs/EXPERIMENT-RUNNER.md`, `experiments/README.md` (rewrite for the new design) |

Verified on the branch: replaying all 1,395 production groups through the
interim engine reproduced every saved request byte for byte and every
checkpoint except the 6 failed review batches (about 26 seconds, no network).
That proves the request-building logic in the components is exact. 203 tests
pass. Demo experiments for visual checks are in `/tmp/dq2` (definitions in
`/tmp/dq2/defs`, runs in `/tmp/dq2/runs`, dashboard on port 8772); they are
disposable.

Decide with the user whether to discard the branch's uncommitted interim code
or build on the branch. Either way, `main` is the stable base.

## Agreed design

### 1. Output contract (the only fixed point)

The pipeline produces **one result per case**, validated at the end:

- **Disposition:** `in_scope`, `excluded` (with a reason), or `uncertain`.
  Reducing uncertain dispositions is a primary experiment goal.
- **Records:** zero or more per case (one per municipal appeal docket), in a
  versioned schema. Every field declares its allowed values (enums, dates,
  counts, docket identifiers, free text) and an explicit `unknown`. Any field,
  including duration, may be unknown when a pipeline does not establish it.
- **Evidence:** fields carry cited quotations (doc, page, quote) when relevant,
  with a strong bias toward evidencing as much as possible. Quotations must be
  verifiable against the frozen source text.
- **Processing status** (complete / failed / in progress, with errors) is run
  metadata stored beside each result, not part of the research output.

Comparisons are always `case id → final result`, across pipelines. The
dashboard's fact-level impact model (`appeal_impact.py`) should read this
contract; its fact catalog should be derived from (or aligned with) the record
schema's field definitions.

### 2. Pipelines are trees of steps, built by Python modules

- A pipeline is a Python module that constructs a tree of step objects with
  their data ("data as code"). Prompts live in files next to the steps that use
  them and are loaded by those steps.
- **Every step owns its behavior and configuration**: its model/provider config
  and prompts (or none, for pure-code steps), an optional selector, optional
  children, and parameters. There is no central recipe and no fixed phases.
- **Composite steps** orchestrate children: sequence, per-case, batched over
  groups chosen by a selector, repeat-until-stable, and so on. Depth is
  unlimited. Today's triage/extract/review/duration structure is one hypothesis
  expressed as steps; experiments may remove duration, add sub-steps, or
  replace triage-then-extract with progressive exclusion.
- **Common interface.** Each step implements:
  - `describe()`: a standardized, diffable descriptor containing the step's
    name and type, a **plain-text description** (what it does, for people and
    visualization), model/provider config, **the full prompt texts** (not only
    hashes), selector and parameters, what it reads and writes, which result
    fields it affects, and the children's descriptors.
  - `run(cases, ctx)`: does the work, calling models only through `ctx`.
- **Selectors** belong to steps: which cases a step works on and how they are
  grouped into calls (one case, sequential groups within a size budget, by
  municipality, ...). Distinguish *members* (cases in a call's context) from
  *targets* (cases whose state the call updates); today's review includes
  finished batch members as context so a retry rebuilds the identical request.
  A component declares the largest group it can handle.
- **Conversations** attach to a group. A step may continue its predecessor's
  conversation only with the same provider and the same group membership.
- **Progressive exclusion:** any step may set a case's disposition to excluded
  (with a reason); later selectors skip excluded cases.
- **Freezing:** a run stores each step's `describe()` output and a hash and
  copy of each step's source code. Diffs show "code changed" for a step whose
  source differs even if its descriptor does not.

### 3. Experiments are modules

An experiment is a Python module that takes the base pipeline tree and returns
a modified tree (remove a step, replace a subtree, change a step's model or
prompt, add steps). The experiment's metadata (hypothesis, base run, cohort,
options such as keeping batch companions or forcing reruns) stays alongside it.
The diff between base and experiment is the difference of their `describe()`
trees plus prompt-text diffs plus source-hash changes. Experiment code is
trusted repository code.

### 4. Model calls are content-addressed

- A call's key is a hash of the provider identity and the full request.
  Identical work is reused regardless of tree shape or step names.
- Runs and resumes re-execute the tree; saved calls make that cheap and
  deterministic (the full production replay took about 26 seconds). Write final
  results, progress, and diagnostics; drop step-specific checkpoint files.
- Keep the `Client` guarantees: reserve each call's worst-case cost and refuse
  it before sending if the cap would be exceeded; never resend failed or
  uncertain deliveries automatically; retries are explicit and archived;
  inherited calls are free and do not count toward the cap; dry runs record
  what each new call could cost without sending.
- Index the production run's saved calls by request hash once, so new
  pipelines can reuse them.

### 5. Providers

Keep `pipeline/providers.py` semantics: a provider renders, sends, reads usage,
prices, bounds before sending, and declares capabilities (conversation, JSON
repair, typed questions). Prices come from `pipeline/models.json`; an unpriced
model cannot make paid calls. A stateless decision model (TypeSafe's Jev: one
state plus typed Choice/Score/Noul questions; answers with probabilities and
confidence; $0.042 per million input tokens, output free; 32k-token limit on
state plus longest question; versioned id `jev-1.13.0`) must fit as a provider
used by some step. Do not implement or pilot Jev yet; a test double proving the
abstraction is enough. Steps own their provider config, so a step may use no
model at all.

### 6. Diagnostics: how a case ran

Store per-case run diagnostics so the dashboard can eventually show how a case
moved through different pipelines: the ordered step trace (step path, which
calls it made with their content keys, inherited or new, outcome, intermediate
values or their hashes, disposition changes). Comparing two runs' traces finds
the first step whose requests diverged. This is diagnostics, not the research
contract.

### 7. Legacy pipeline

- The current pipeline is legacy code to adapt, not a structure to stay bound
  to. Do not preserve its checkpoint layout, call names or status vocabulary in
  new runs.
- Convert the production run's outputs once into the new per-case result format
  (disposition + records + evidence); that is the baseline for comparisons.
  `appeal_contracts.adapt_run` already reads it and knows its scope categories
  (confirmed / uncertain / excluded) and finalized records.
- **Port the current pipeline as the first pipeline module** that builds the
  same requests as the legacy run (the interim components prove this is
  possible). Then production's saved calls are reused for free and a targeted
  experiment pays only for the steps it changes. Acceptance check: the ported
  module, fed production's saved calls with sending disabled, reproduces
  production's final results. If part of the port fits the new structure badly,
  dropping request identity for that part is acceptable; it only loses free
  reuse there.
- Legacy details the port must reproduce: triage request = system prompt plus
  `source + "\n\n" + triage prompt`; extraction continues the triage
  conversation with the *validated* triage JSON (`json.dumps`, ASCII-escaped)
  as the assistant turn; review batches up to 2 cases in run order within
  200,000 characters (`len(source) + len(json.dumps(candidate))`), with every
  drafted case including finished ones as context; review's system prompt is
  primer + extraction, scope and editing contracts + batch instructions;
  duration continues the review conversation with the raw review answer and
  targets only successfully reviewed cases; settings `reasoning_effort: high`,
  `max_tokens: 65536`, JSON object output. 214 of production's 228 duration
  calls were saved under an older name without a request hash; content
  addressing makes names irrelevant. `release-check` used 6-case review batches
  (an older finishing structure).

### 8. Runner and dashboard

- Runner flow stays: select a cohort, plan (load the experiment module, build
  and validate the tree, freeze descriptors and sources, dry-run estimate with
  no credentials), run under a cap on new spending, finalize into baseline and
  candidate results for the same cases, compare. Keep batch companions for
  cohort cases unless grouping is the variable. Keep retrospective comparisons.
- Dashboard: keep the experiments index, impact, case queue, case detail with
  evidence, cost, and baseline quality profile. Replace the "What changed" view
  with a tree of steps from `describe()`: plain-text descriptions, models,
  prompts, selectors, affected fields; highlight added, removed and changed
  steps; show prompt and parameter diffs per step. Later: per-case trace
  comparison.

## Suggested build order

1. Output contract: record schema with per-field allowed values, evidence,
   disposition; converter from legacy runs; validation; adapt `appeal_impact`.
2. Step interface, composite steps, selectors, `describe()`, freezing, diffing.
3. Content-addressed call store over the existing `Client`; index production
   calls by request hash.
4. Port the legacy pipeline as the first module; acceptance replay against
   production with sending disabled.
5. Runner on modules: plan, dry run, run, finalize, compare; per-case traces.
6. Dashboard tree view and trace data; docs rewritten; remove interim recipe
   code.

## Open details to settle while building

- Exact record schema field list and allowed values (start from the v6 record
  shape and `appeal_impact.py`'s fact catalog).
- Whether uncertain cases keep records. In production they do: 131 of the 513
  finalized records belong to uncertain-scope cases.
- Selector parameter conventions and how components declare their group limits.
- Where pipeline and experiment modules live (for example `pipelines/` and
  `experiments/NAME/experiment.py` beside its metadata and prompts).
