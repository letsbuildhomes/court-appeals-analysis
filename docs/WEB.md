# Web app

One read-only web app shows everything: runs as they happen, the lab, every
dataset with a viewer for its kind, and pipelines.

```sh
echo 'a long password' > .web-password    # once; the file is ignored by git
make web                                  # python -m court web; http://SERVER:8771/
```

`SERVER` is this machine's private-network address; the app listens on all
interfaces and prints its URL. It changes nothing and needs no model
credentials.

## Password

The whole site is behind one password. It comes from `COURT_WEB_PASSWORD`,
or the first line of `.web-password` (`--password-file` names another file);
without one, the app refuses to start unless given `--no-password`. Signed
out, every page shows a sign-in form and the API answers 401. Signing in sets
a cookie for 30 days: HttpOnly, SameSite=Strict, and signed with a key derived
from the password, so changing the password signs everyone out while
restarting the app does not. Repeated wrong passwords from one address are
answered more and more slowly. **Sign out** is in the header.

The app is plain HTTP on the private network; the password keeps casual
visitors out, but anyone who can watch that network's traffic could read it.

## Sections

| Section | What it shows |
| --- | --- |
| **Overview** | Running and recent runs, production's numbers, experiments, named datasets |
| **Runs** | Every run, full or an experiment's subset, newest first; running ones refresh every two seconds |
| **Lab** | Experiment rounds (verdicts, quality measures, what changed, cost) and production's quality profile |
| **Data** | Every dataset by kind, with its report (grouping report, text audit, results profile) and items |
| **Pipelines** | Resolved pipelines, their step trees, prompts and settings, tag history, and diffs between any two |

## Watching a run

A run's page shows, while it runs:

- **Now:** elapsed time, items, new spending against the cap (and money
  reserved by calls still in flight), the value of saved calls reused,
  calls sent and failed, and tokens in (cached) and out.
- **Spending over time:** each delivered call's charge, cumulative, under the
  cap's line.
- **Steps:** the pipeline's steps from where the run starts: waiting, running
  or done; items in and out, failures by kind, how many items the running step
  has touched with an estimate of the time left; and each step's calls,
  spending and tokens.
- **Items:** where each case is, or how it ended; select one for its trace:
  every call (reused or new, with batch partners), warning and rejection.

The runner writes `data/runs/NAME/run.json` when a run starts,
`progress.json` once a second while it runs, and every call's reservation
and charge to `ledger.jsonl` as it happens; the page reads only these. A run
whose process ended while running shows as interrupted; running the same name
again continues it. Experiment rounds are ordinary runs labeled with their
experiment, round and side.

## Stage viewers

From **Data**, each kind of item has its own view:

- **Documents:** listing entries and the PDF's pages beside their text.
- **Pages:** the PDF page beside its selected text, with status, source,
  reason and OCR confidence; step through the document.
- **Cases:** each document's caption and listing dockets, basis and flags,
  and its pages beside the case text the model receives. This is where
  grouping flags are judged.
- **Results:** the disposition, every record's facts, and the evidence for
  each, highlighted in the case text.
