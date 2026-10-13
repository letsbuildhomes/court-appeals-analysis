# Architecture

Status: implemented, October 12, 2026; "Where things stand" lists what
remains. [CODE-ARCHITECTURE.md](CODE-ARCHITECTURE.md)
describes how the Python project is organized.

## What this project does

We study municipal housing appeals heard by the Vermont Environmental Division.
The court publishes its decisions as PDFs. We turn those PDFs into one row of
structured research data per case: is it a municipal housing appeal, who
appealed, what was decided, how long it took, with a quote from the source
backing each answer.

Most of the work is reading. A language model does the reading, so the
answers are only as good as the prompts and steps around it. Improving them
is where most of the effort goes.

## The shape of the system

There are three parts, and they never reach into each other:

```text
                    ┌──────────────── the pipeline ────────────────┐
 court website ──▶  fetch ──▶ text ──▶ cases ──▶ extract  ──▶  results map
                    └──────────────────────────────────────────────┘
                                                                   │
                                     ┌─────────────────────────────┴───┐
                                     ▼                                 ▼
                                  the lab                       research access
                     tests changes to the pipeline,         MCP server and viewer
                     compares their outputs                 over one chosen map
```

1. **The pipeline** turns raw PDFs into structured data.
2. **The lab** tests changes to the pipeline, anywhere in it, and compares
   what they produce.
3. **Research access** lets people query and browse the finished data.

The pipeline doesn't know the lab or research access exist. The lab and
research access only read the pipeline's saved outputs, mostly the final
results map.

## Steps: the one idea

The pipeline is built from one nestable building block, the **step**.

- A step takes keyed data in and returns keyed data out: maps from a key,
  such as a document hash or a case ID, to a value.
- It declares the contract of its inputs and its output.
- How it gets from input to output is its own business. It can process items
  one at a time, batch them, group them, call a model, or hand the data to
  child steps and tidy up what they return.
- Each output item is either a value or a failure with its reason. One item
  failing never stops the others.

Steps nest, and a pipeline is just a tree of steps. Today's pipeline has four
top-level children, which this document calls **stages**: fetch, text, cases
and extract. "Stage" is only a way of talking about the top of the tree.
Those names, and names deeper in the tree, come from the pipeline's
definition, not from the code. A position in the tree is a slash path, such as
`extract/finishing/review`. Nothing in the design assumes items are processed
one at a time, in a fixed order or in batches; those are choices individual
steps make.

Steps don't do everything themselves. They use **services** the runner
provides:

| Service | What it does |
| --- | --- |
| Models | The only way to call a language model. Applies the spending cap, the retry policy and the call cache. A call is stored under a hash of the model and the exact request, so an identical request is answered from the cache for free. |
| Cache | Lets expensive code work, such as OCR, be saved per item and reused when its input hasn't changed. |
| Trace | Records what happened to each item: steps, calls, outcomes. |
| Checkpoints | Saves chosen step outputs as datasets. Today's stage boundaries are always saved, and any other step's output can be saved too. |

Because of the caches, resuming an interrupted run just means running it
again. Finished work comes back from the cache, and nothing else needs to
remember where the run stopped.

## Every version survives

Experiments create parallel versions of the pipeline, and every version that
has ever run must stay runnable. Four pieces make that possible, and nothing
in them is ever overwritten.

- **The step library.** Every step implementation is a library entry named
  *family*`:`*variant*. The family is the job it does and the variant is
  the particular way it does it, for example `review:legacy`,
  `review:strict-citations` or `ocr:tesseract`. Families can be grouped
  with slashes, as in `shared/download:standard`, and the colon always marks
  the variant. Library names are separate from tree positions: the position
  `extract/finishing/review` can hold `review:legacy` in one pipeline and
  `review:strict-citations` in another. Each entry defines exactly one step. Once any pipeline has
  used an entry, it is never edited; a new idea is a new variant, named for
  what it does rather than numbered.
- **The prompt library.** Prompts are versioned data with the same naming,
  such as `review:strict-citations`, so changing a prompt never needs new
  code.
- **Pipeline definitions.** A pipeline is data, not code: a tree of positions,
  each naming a library entry, a prompt and settings. A definition is
  usually written as a base pipeline, identified by its ID, plus replacements
  at chosen paths. The system resolves it into the full tree and saves it.
  The hash of the resolved tree is the **pipeline ID**.
- **Tags.** Names such as `production` point at pipeline IDs. Tags are an
  append-only log, so the history of every tag is kept.

A step can only break if something underneath it changes, so everything
underneath it is versioned:

- The **step interface**, meaning what a step receives, what it must return
  and which services it can use, has a version number. Each library entry
  declares the interface it targets. The framework can change internally as
  long as every interface version in use still works. A breaking change adds
  a new interface version alongside the old one.
- **Contracts** for saved data are versioned the same way.
- **Python packages** are pinned by the lockfile, whose hash is recorded with
  each pipeline, along with the git commit it was created at.
- **Models** can't be versioned. The call cache is what makes their answers
  reproducible.

A library entry may depend only on the framework, the contracts and other
library entries. Helpers shared between steps are library entries too.

## The stages

These are the top-level steps of today's pipeline.

| Stage | Reads | Writes | Reproducible? |
| --- | --- | --- | --- |
| fetch | a court website | **documents**: PDF files named by their hash, plus listing metadata (title, date, docket text, URL) | Yes, for the same listing snapshot |
| text | documents | **pages**: the plain text of every page, with how it was obtained (native or OCR) | Yes; OCR can differ slightly across tool versions |
| cases | documents, pages | **case set**: case ID → its docket numbers, its documents and its full text | Yes, exactly |
| extract | case set | **results map**: case ID → structured result | Roughly: models vary, but identical calls are reused from the cache |

### Fetch

A fetch step knows one source: how to walk its listing, and which PDFs it
links to. A shared downloader verifies the files and stores them by content
hash. Today there is one source, the Environmental Division. Adding the
Vermont Supreme Court would mean one new source step. The later stages would
stay the same, apart from grouping rules that need to know the new court's
docket format. Experiments normally start from documents already fetched.

### Text

Text makes no model calls. It needs Poppler and Tesseract installed, like any
other dependency (see "Running it"). For each PDF page it extracts native
text, decides whether that text is usable, OCRs the pages that aren't, and
selects one text per page. An audit reports pages with no usable text. The
native-text QA experiment and the page-versus-text viewer are tools *about*
this stage's output. They aren't steps in it.

### Cases

This stage defines what a "case" is, so everything downstream rests on it.

- Each PDF's docket numbers are read from its first-page caption. The court
  listing's docket field is the fallback.
- PDFs sharing a docket number form one group. A caption that lists several
  dockets joins their groups, which is how related appeals end up together.
- Near misses, such as a listing that disagrees with the caption, are recorded
  as flags and never merge groups.
- Each case's text is its documents' page texts in order, marked with document
  and page numbers. Quotes in results cite those numbers.

A case's ID comes from its first docket number, for example `g-1-1-09-vtec`.
Today there are 1,395 cases; 224 of them join several docket numbers.

**Checking the grouping.** Every case-set build writes a grouping report:
- counts of cases, documents and multi-docket cases
- every flag, by kind, with the documents involved
- documents with no caption docket number
- unusually large groups

The web app's case view shows a flagged case's documents side by side, so a person
can decide whether the flag is a real problem. Extraction adds a second
signal: its first step checks whether each document belongs to the case, and
those answers are collected back into the report. To improve grouping, a
person changes a rule and reads the **case-set diff**: which cases split,
merged, gained or lost documents, or stayed identical. Only changed cases
need new extraction.

### Extract

Extract turns a case set into a results map. Every result follows one
**output contract**:

- a disposition (in scope, excluded with a reason, or uncertain)
- one record per docket, where every field has defined values plus an
  explicit `unknown`
- evidence quotes, which are checked against the case text

Extract is where most experiments happen, so its inner steps are ordinary
library entries with declared inputs and outputs: triage, record
extraction, review, duration and so on. The legacy production extraction
is preserved step for step as `legacy` variants, with its prompts.

## Datasets

Saved step outputs are **datasets**: documents, pages, case sets, results
maps, and any intermediate output saved for an experiment. They all follow
the same identity rule.

- Each item has a stable **key**: a document's hash, a page number, a case ID.
- Each item has a **content hash**.
- The dataset's ID is a hash of its keys and content hashes. It doesn't depend
  on the code, rules or run that produced the dataset. Identical contents give
  the same ID. If a grouping rule changes but no case changes, the case set
  keeps its ID.
- The manifest records the producer separately: which pipeline ID, from
  which input datasets, and when.

So any two datasets of the same kind can be compared item by item. Matching
key and hash means the item is identical. A matching key with a different
hash means it changed. A missing key means the item was added or removed.

**Results maps** are the datasets the lab and research access depend on most.
Every map has the same format, however it was produced:

- the legacy production run, converted once into a map
- a candidate pipeline's run on 12 cases
- the hand-checked reference answers, described below
- the final full run

A pipeline rewritten from scratch can still be compared with the legacy
run. Both produce maps, and the comparison is by case ID.

## The lab

The lab answers one question: is pipeline B better than pipeline A?

- **Hypothesis.** Any change to a pipeline definition at any depth: a prompt,
  a model, a setting, a grouping rule, a replaced or added step. New step
  variants and prompts go straight into the libraries.
- **Run.** A pipeline applied to inputs. A run can start from any saved
  dataset and be limited to chosen keys. Only work whose inputs changed is
  recomputed, and the caches answer everything else.
- **Reference map.** About 50 cases, answered by a strong model working
  slowly with the full source, then checked. It uses the output contract, with
  "not labeled" allowed for fields nobody checked. These answers count as
  correct. If a grouping change splits or merges a reference case, that case
  needs relabeling; the case-set diff shows when.
- **Comparison.** The final results are judged: for two maps, every shared
  case ID is compared fact by fact (scope, outcome, duration and so on).
  Against the reference map, "improved" means "closer to the reference
  answer". Between two runs without a reference it means "more specific",
  which still needs a person's judgment.
- **Diffs.** Two kinds explain a result:
  - The *definition diff* shows which positions in the tree changed, and to
    which variants, prompts and settings.
  - The *data diff* compares the two pipelines' outputs at every step they
    share, so you can see where a change took effect. For example: triage
    changed for 4 cases, extraction for 3 of those, and the final results
    for 2.
- **Experiment.** A record holding a hypothesis, the pipeline it tests and
  its results. It runs in two rounds:
  1. **Probe** on the reference cases the change targets. Cheap; stop here if
     it doesn't help.
  2. **Confirm** on the rest of the reference set, checking for regressions.
     Probe work comes from the cache.
- **Adoption.** Moving the `production` tag to the tested pipeline's ID.
  Nothing is copied or overwritten. Moving the tag requires a confirmed
  experiment, so every adopted pipeline has results for all reference cases.
  The tag's log is the pipeline's history.
- **Web app.** One read-only app ([WEB.md](WEB.md)) for watching runs live with
  their spending, the lab's experiments and comparisons, a viewer for every
  dataset, and pipelines with their trees and diffs.

The lab never runs the corpus repeatedly. After a series of adoptions, the
last step is one full run of the adopted pipeline over every case.

## Research access

Research tools read one chosen results map, and the case set it came from
for showing evidence. They know nothing else about the pipeline.

- **Snapshot.** The chosen map, frozen into a read-only SQL database of
  research facts.
- **MCP server.** Lets Claude or another client query the snapshot.
- **Browsing.** The web app shows any results map's answers with their quotes
  in the case text.

Publishing new data means building a snapshot from a different map.

## How the repository is laid out

```text
court/          the Python package
  core/         step interface, services, runner, library loader, datasets
  contracts/    the saved-data contracts: documents, pages, case sets, results
  lab/          comparison, experiments, tags
  reports/      grouping report, case-set diff, text audit
  ui/           the web app: live runs, the lab, dataset viewers, pipelines
  research/     snapshot, MCP server
steps/          the step library: family:variant entries, never edited once used
prompts/        the prompt library, same naming
pipelines/      pipeline definitions, their resolved trees by ID, and the tag log
experiments/    experiment records: hypothesis, pipeline, results
```

The package is the framework and the tools built on it. The libraries and
definitions are content the framework loads. The lab and research access use
only `core` and `contracts`, and they never import a library entry.

## Running it

The system runs as ordinary Python on any machine with Poppler and Tesseract
installed. One Docker image packages the whole system with every dependency.
Any command can run inside it, with the repository and data directory
mounted. No stage requires Docker.

## Where things stand

- **Reproduced.** The `legacy` pipeline runs from the September 22 listing
  snapshot through results without network access or model calls: documents,
  pages and the case set are identical to the legacy datasets, its 1,385
  results identical, and the same 10 cases fail at review. Saved downloads,
  page text and OCR results were imported into the caches; the legacy run
  is recorded as the run `legacy`.
- **Text in the image.** The Docker image has the legacy run's tool versions
  (Poppler 22.12.0, Tesseract 5.3.0). Rerun from scratch on 40 documents
  (311 pages), the text stage gives every page the same status and source;
  native, blank and unresolved pages are identical, and 83 of 85 OCR pages
  have identical text, with confidence differing only in late decimals.
  Tesseract is not perfectly repeatable, so OCR stays cached per page.
- **Fixed on the way.** The grouping code ordered one case's flags by Python
  set order, which changes between runs; flags now follow caption order.
- **Next.**
  - Choose and label the reference set.
  - Refine the grouping. The web app's case view shows the first issue: a caption whose
    layout puts `}` between the number and "Vtec" is read as no docket.
  - Decide whether new review steps keep batching (see [LAB.md](LAB.md)).
  - Run experiments, then one final full run.
- **Shelved.** A sandbox and simulated-answer walkthrough for the lab is in a
  git stash (`git stash list`: "walkthrough: sandbox…"; commit `3d7cef0`).
  Rebuild it as a fake models service: the tests already use one.

Existing legacy data was never modified. It is read where it is, and
new datasets are written beside it.
