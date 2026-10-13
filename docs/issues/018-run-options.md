# Spending cap, resends and parallel calls behaved unexpectedly

- Status: done
- Opened: 2026-10-13
- Needed: before experiments

**What:** Continuing the bracket-columns probe to resend one failed call was
refused at once: the spending cap counted everything the run had spent
across executions, though the help text said "newly spend". A call could be
resent only once per run, ever. `lab run` accepted `--resend-max-tokens` but
ignored it. Steps sent at most 4 calls at a time, so a whole run took far
longer than the provider's limits require.

## Outcome

The cap limits one execution; a refusal says what was spent and what the
call reserves. `--resend-failed` resends each failed call once per execution.
`lab run` passes `--resend-max-tokens` on. Steps send up to 16 calls at a
time by default (`--workers`).
