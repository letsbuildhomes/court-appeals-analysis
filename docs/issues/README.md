# Issues

Open questions, known problems and planned work for this project, one
Markdown file per issue. Agents keep these files up to date as working notes;
the user doesn't follow them.

When talking to the user, never refer to an issue by number or file name, and
don't assume they know its contents. Describe the problem itself in plain
words, as if raising it for the first time. Numbers are for links between
issue files and for finding them.

## Format

Name files `NNN-short-slug.md`, numbered in the order they were opened. Each
file starts with:

```markdown
# Title

- Status: open | decided | done | dropped
- Opened: YYYY-MM-DD
- Needed: before experiments | before the final run | any time
```

Then, in plain language for someone who hasn't thought about the project in
weeks:

- **What:** the problem or question.
- **Why it matters:** what goes wrong, or what we can't do, until it's resolved.
- **Options:** the realistic choices, if there's a decision to make.
- **Recommendation:** what we'd do, and why.

When an issue is resolved, set its status and add a short **Outcome** section
saying what was decided or done. Close it in the same commit as the work, so
that commit is the record; git finds it from the file's history. Name a
commit only when the work landed earlier than the commit that closes the
issue, and name the experiment when an experiment decided it.
Don't delete closed issues.

## Finding issues

```sh
grep -l 'Status: open' docs/issues/*.md
grep -l 'Needed: before experiments' docs/issues/*.md
git log -S 'Status: done' --format='%h %s' -- docs/issues/NNN-short-slug.md   # the commit that closed it
```
