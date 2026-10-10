# Pipeline architecture

The extraction pipeline is a **recipe**: configuration naming providers,
prompts, steps and phases. The engine (`pipeline/engine.py`) runs any valid
recipe, and an experiment is a set of files composed over a base recipe. The
production recipe is `recipes/appeals/recipe.json`.

```text
first_pass (one case at a time)                 finishing (batched: up to 2 cases per call)
  triage ──continues──▶ extract        ⟶          review ──continues──▶ duration
```

## Concepts

| Concept | Where | What it is |
| --- | --- | --- |
| Recipe | `recipes/appeals/recipe.json`, `pipeline/recipe.py` | Providers, prompts, steps, phases and their order |
| Step | `recipe.steps.NAME` | A component with a provider, settings, prompts and a conversation mode |
| Component | `pipeline/components/*.py` | The code a step runs: request building, validation, how the answer updates a case |
| Phase | `recipe.phases.NAME` | Steps run in order, either per case or per batch |
| Batcher | `pipeline/batching.py` | How a batched phase groups cases into calls |
| Conversation | `pipeline/conversation.py` | An immutable transcript a step can continue |
| Provider | `pipeline/providers.py`, `pipeline/models.json` | How requests are sent and priced |

A recipe has one **per-case** phase followed by one **batched** phase. The
per-case phase writes `first_pass/<case>.json`; a case it finishes without a
draft record is complete (for example, an excluded appeal). Cases with drafts
go to the batched phase, which writes `results/<case>.json`. Finalized records
need the duration bounds the batched phase produces.

### Steps and what they affect

| Step | Prompts (role = name) | Conversation | Research facts it affects |
| --- | --- | --- | --- |
| triage | system = system, instructions = triage | fresh | Scope category, municipal appeal, housing |
| extract | system = system, instructions = extract | continues triage | Municipal decision and date, litigation and permission status, discretion, appellants and representation, municipality, proposed units and lots, restrictions, housing changes, related dockets |
| review | primer, extract, triage, review | fresh | Scope (by flagging) and every extracted fact (by correcting) |
| duration | instructions = duration | continues review | Appeal duration, start date, end date |

Review's system prompt concatenates the primer with the extraction, scope and
editing contracts, so editing the extract or triage prompts also changes review
requests. The dashboard's *What changed* view draws this table from each run's
recipe and highlights what an experiment overrides.

### Conversations

A step's `context` is `fresh` or `continue`. Continuing appends to the previous
step's conversation in the same phase and unit, so the earlier messages stay a
byte-identical prefix that providers can serve from cache. Extraction sees the
validated triage answer; duration sees the review answer and refers back to the
sources already in the review request. A fresh extraction restates the source
and scope decision; a fresh duration call carries its own sources. A
conversation belongs to one provider, so a continuing step must use the same
provider as the step before it.

### Batching

A batched phase's `batch` setting chooses a batcher: `sequential` packs cases
in run order up to `max_cases` and `max_chars` (production: 2 cases, 200,000
characters), and `single` gives each case its own call. Batch members share one
request, so a case's batch companions affect its answer; the experiment runner
keeps companions unless batching is changed on purpose.

### Calls, reuse and spending

Every model call goes through `appeal_runtime.Client`, which saves the request,
response, usage and charge under a stable name: `<case>.<step>` for per-case
steps and `<step>-<batch id>` for batched steps, plus a request hash for steps
whose requests depend on earlier answers (duration). An identical saved request
is reused for free; a changed one under the same name is refused. Before each
call the client reserves the provider's worst-case cost and refuses the call if
the run's cap would be exceeded, so nothing is sent. Failed or uncertain
deliveries are never resent automatically.

With `inherit`, a client reads through to another run's calls and copies an
identical, successful call instead of making it; inherited calls are free and
do not count toward the cap. This is how experiments reuse unchanged work.
`tests/test_pipeline_replay.py` rebuilds the production run's requests through
this path and requires every one to match.

## Providers

A provider renders requests, sends them, reads usage, prices it, and bounds a
request's cost before it is sent. `pipeline/models.json` lists each model with
its provider type and prices; a model without a price cannot make paid calls.

| Type | Class | Requests | Conversations | Pricing |
| --- | --- | --- | --- | --- |
| `chat-completions` | `ChatCompletionsProvider` | OpenAI-compatible messages with JSON output | yes | input, cached input, output tokens |
| `decision` | `DecisionProvider` | `{"model", "state", "questions"}` with typed questions | no | input tokens (output priced if listed) |

A recipe's provider entry names a model and the environment prefix for its
endpoint and key (`APPEALS` reads `APPEALS_API_BASE_URL` or `APPEALS_API_URL`
and `APPEALS_API_KEY`). Components declare the provider kinds they accept;
validation refuses a step whose provider cannot serve it.

### Adding a model

1. Add it to `pipeline/models.json` with its provider type and prices.
2. Reference it from a recipe provider, with an `env` prefix for credentials.
3. If its API differs from both provider types, add a `Provider` subclass
   implementing `render`, `post`, `usage`, `price`, `outcome`, `decode`,
   `bound` and `estimate`, and register it in `PROVIDERS`.

A decision model such as TypeSafe's Jev answers typed questions about one state
and returns probabilities and confidence instead of generated text. It fits the
`decision` type: stateless, priced on input tokens, and limited in state size,
so a step must build a compact state. Using one needs a component that turns a
case into a state and questions and maps the answers to the step's outputs;
its results then compare through the same research facts. No decision model is
configured yet.

## Components

A component subclasses `pipeline.components.Component` and registers itself:

```python
from pipeline.components import register
from pipeline.components.review import Review

@register
class StrictReview(Review):
    name = 'strict-review'
    summary = 'Reviews drafts and rejects any change without a verbatim quotation.'

    def apply(self, unit, case, result, value):
        ...  # validate more strictly, then call super().apply(...)
```

Class attributes describe it for validation and the dashboard: `scope`
(`case` or `batch`), `activity`, `provider_kinds`, `prompt_roles`,
`fresh_roles`, `reads`, `writes`, `facts` (keys from `appeal_impact.py`),
`failure_status`, and naming flags. Per-case components implement `request`
and `accept`; batched components implement `request`, `check` and `apply`.
`transcript` sets what a continuing step sees as this step's answer.

An experiment can add components in its `components/` directory; they are
loaded when the experiment is planned and copied into its run.

## Runs and recipes

`appeals.py plan` freezes the composed base recipe into each new run
(`recipe.json`, `prompts.json`, `components/`). Runs made before recipes have
no `recipe.json`; `recipe.legacy_recipe` rebuilds theirs from the manifest, and
the replay test confirms it reproduces their requests. Canonical artifacts
(`appeal_contracts.adapt_run`) carry `recipe.describe()` output, which the
dashboard draws.
