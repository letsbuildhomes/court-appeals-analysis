# Pipeline architecture

A pipeline is a Python module that builds a **tree of steps**. Every step owns
its behavior and configuration: a plain-text description, an optional model
and prompts, an optional selector, parameters and, for composite steps,
children. There is no central configuration and no fixed phases. Whatever the
tree looks like, it produces **one result per case** under the output contract
([PIPELINE_CONTRACT.md](../PIPELINE_CONTRACT.md)).

The first pipeline is the production pipeline, ported as steps in
`pipeline/legacy/`:

```text
appeals                        Sequence
├── size-limit                 stop sources over 600,000 characters (code)
├── first-pass                 per case
│   ├── triage                 scope; a "no" excludes the case
│   └── extract                draft records; continues triage's conversation
├── finishing                  batches of up to 2 drafted cases within 200,000 characters
│   ├── review                 correct drafts, or flag scope (uncertain)
│   └── duration               evidenced bounds; continues review's conversation
└── assemble                   contract records from drafts and bounds (code)
```

It builds the same requests as the legacy run, so that run's saved calls are
reused for free: replaying all 1,395 production cases with sending disabled
reproduces every result in about 25 seconds (`tests/test_pipeline_replay.py`,
`REPLAY_ALL=1`).

## Concepts

| Concept | Where | What it is |
| --- | --- | --- |
| Step | `pipeline/steps.py` | `describe()` returns a diffable descriptor; `run(cases, ctx)` does the work |
| Model step | `ModelStep` | One call per group: `request` builds it, `accept` validates the answer and updates the targets |
| Code step | `CodeStep` | Updates cases with code; no model |
| Composite step | `Sequence`, `Grouped`, `per_case` | Runs children in order; `Grouped` runs them within each group its selector forms |
| Selector | `pipeline/selectors.py` | Which cases a step works on and how they are grouped into calls |
| Case and group | `pipeline/cases.py` | A case's state while it runs; a group's members (in context) and targets (updated) |
| Conversation | `pipeline/conversation.py` | An immutable transcript bound to a group |
| Provider | `pipeline/providers.py`, `pipeline/models.json` | How requests are rendered, sent, priced and bounded |
| Call store | `pipeline/store.py` | Content-addressed calls, the spending cap, retries, indexes of earlier runs |
| Engine | `pipeline/engine.py` | Runs a tree; the context through which steps reach models; turns each case into a result |
| Registry | `pipeline/registry.py` | Pipeline IDs, frozen snapshots, tags, diffs |
| Runs | `pipeline/runs.py` | Run directories: results, statuses, traces, ledger |
| Corpus | `pipeline/corpus.py` | The frozen source text every pipeline reads |

### Steps

```python
from pipeline.legacy.review import Review

class StrictReview(Review):
    description = 'Reviews drafts and rejects any change without a verbatim quotation.'

    def accept(self, group, value):
        outcomes = super().accept(group, value)
        ...  # replace an outcome with an exception to fail that case
        return outcomes
```

Class attributes describe a step: `description`, `reads` and `writes` (case
values), `fields` (the research facts it affects, keys from the fact catalog),
`max_group` (the largest group it can handle), `prompt_names`, `defaults`
(parameters) and `code` (other files its behavior depends on, hashed into its
descriptor). A model step's `request(group, conversation, provider)` returns
the request and the conversation it extends; `accept(group, value)` validates
without changing anything and returns, per target case, an update or an
exception. Raising rejects the whole answer.

A step's prompts are files next to the step, loaded as `Prompt` objects; its
model is a `Model(name, env, **settings)`. `describe()` includes the full prompt
texts, the model and its prices, the selector, parameters, and the hash of
every file that implements the step.

### Selectors, groups and conversations

A selector takes the active cases in run order and returns groups:
`selectors.each()` gives one case per call; `selectors.sequential(max_cases,
max_chars, measure, requires)` packs cases in order within a size budget, as
production's review did. Validation refuses a selector whose groups exceed a
step's `max_group`.

Members are the cases in a call's context; targets are the cases it updates.
Review includes every member of its batch, and duration targets only the cases
review accepted, while still continuing review's conversation with all members.

A conversation belongs to a group. A step continues its predecessor's
conversation (`continues=True`) only within a `Grouped` step, with the same
model, and only if the provider supports conversations.

`Grouped(..., wait=True)` runs nothing while any case stopped earlier for a
reason a retry could change, so retried cases rejoin the same batches.

### Case state

A case is active until a step **excludes** it (a disposition with a reason),
**finishes** it early (complete, no further processing), or **fails** it.
Selectors only pick active cases, so any step can exclude cases progressively.
In a dry run, a case whose next call would be new is left **pending**.

At the end the engine turns each case into a result (disposition and records)
and a processing status (complete, failed or in progress, errors, warnings,
quote verification). A complete case must have a disposition, and every result
is validated against the contract.

## Calls, reuse and spending

A call's key is the hash of the provider identity, the full request and a
sample number. Identical work is reused whatever the tree's shape or step
names. A run re-executes its whole tree; saved calls make that cheap and
deterministic, so there are no step checkpoints to resume from.

The store (`data/calls/`) keeps every call once. It reads through to indexes of
earlier runs' saved calls: `data/calls/index/production.json` maps the
production run's delivered calls to their files, which are never modified. A
call that validation rejected is still a delivered answer and is reused; an
incomplete or failed delivery is not.

Each run's `Calls` view keeps the guarantees of the earlier client:

- Before each new call it reserves the worst-case cost and refuses the call if
  the run's cap on new spending would be exceeded. Nothing is sent.
- A failed or uncertain delivery is never resent automatically. With
  `--retry-failed`, each failed call, or call whose answer validation rejected,
  is resent at most once per execution; earlier attempts are archived with
  their charges.
- Reused calls are free and do not count toward the cap.
- A dry run records what each new call could cost and does not send.

Sample 0 is the default. Asking for another sample of a step's requests
(an experiment's `samples`) measures run-to-run variation; later steps pay only
when that sample's answer differs.

## Pipeline IDs, tags and diffs

A pipeline's ID (`p-` and 12 hex digits) is the hash of its descriptor, so it
covers configuration, prompt texts and code. `registry.freeze` stores the
descriptor and copies of the source files once in `data/pipelines/<id>/`, with
how to rebuild the tree: a module and options, or an experiment applied to a
base.

Tags name pipelines. `pipeline/tags.jsonl` (in git) is an append-only log; a
tag's current target is its latest entry, and earlier entries are its history.
Experiments and comparisons record resolved IDs, so moving `production` never
changes what an earlier comparison measured.

```sh
uv run --inexact python appeal_runner.py tags
uv run --inexact python appeal_runner.py pipelines
uv run --inexact python appeal_runner.py diff production p-…
uv run --inexact python appeal_runner.py tag production p-… --note "Adopted X" --experiment NAME
```

`registry.diff` compares two descriptors step by step: added, removed and
changed steps, changed configuration fields, prompt changes with both texts,
changed source files and reordered children. The dashboard draws it as a tree.

A code change gives a pipeline a new ID even when its requests are identical;
its calls are still reused, and the diff shows "code changed".

## Traces

Each case's trace (`traces/<case>.json`) lists what happened to it in order:
each call with its step path, content key, whether it was new or reused, the
other cases in its group, and a hash of the answer; rejected answers;
disposition changes; failures; code steps. Comparing two runs' traces finds the
first step whose requests diverged.

## Providers

A provider renders requests, sends them, reads usage, prices it, and bounds a
request's cost before it is sent. `pipeline/models.json` lists each model with
its provider type and prices; a model without a price cannot make paid calls.

| Type | Class | Requests | Conversations | Pricing |
| --- | --- | --- | --- | --- |
| `chat-completions` | `ChatCompletionsProvider` | OpenAI-compatible messages with JSON output | yes | input, cached input, output tokens |
| `decision` | `DecisionProvider` | `{"model", "state", "questions"}` with typed questions | no | input tokens (output priced if listed) |

A model's `env` prefix names its credentials (`APPEALS` reads
`APPEALS_API_BASE_URL` or `APPEALS_API_URL`, and `APPEALS_API_KEY`).

A decision model such as TypeSafe's Jev answers typed questions about one state
and returns probabilities and confidence. A step that uses one builds a compact
state and questions in `request` and maps the answers in `accept`; it cannot
continue a conversation. `tests/test_pipeline.py` exercises this with a test
double. No decision model is configured.

### Adding a model

1. Add it to `pipeline/models.json` with its provider type and prices.
2. Give a step `Model('name', 'PREFIX', ...)`, with credentials under that prefix.
3. If its API differs from both provider types, add a `Provider` subclass
   implementing `render`, `post`, `usage`, `price`, `outcome`, `decode`,
   `bound` and `estimate`, and register it in `PROVIDERS`.

## Legacy runs

`pipeline/legacy/convert.py` converts a legacy `appeals-1` run's saved outputs
into a converted run, described by the ported pipeline built with that run's
frozen prompts and settings (`appeal_runner.py import-legacy`). `appeals.py`
still exports, checks and browses legacy run directories; new runs go through
`appeal_runner.py`.
