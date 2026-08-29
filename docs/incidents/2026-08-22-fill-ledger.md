# A risk control that failed closed for 23 hours

2026-08-21 → 2026-08-22. Demo environment, no funds at risk.

A position cap in the executor's risk gate latched shut and rejected 4,233 valid orders over
roughly 23 hours, against zero open positions on the exchange for the entire period. Nothing lost
money, since the account was a Kalshi demo account and the order path is fill-or-kill only, but a
defence-in-depth control failed *closed* against risk that did not exist, which erodes trust in a
kill switch the same way failing open erodes trust in a firewall. An operator who watches a cap
refuse orders it should not be refusing learns to ignore it.

This is the write-up because the root cause is a data structure that only ever grew, and because
the first fix was wrong in an instructive way.

## What broke

`RiskGate` tracked exposure in two plain dicts, `_position_by_ticker` and
`_position_by_correlation_group`. `record_fill()` added each real fill's `fill_count` into both.
Nothing anywhere in the codebase ever subtracted, with no decrement on close, cancel, expiry, or
settlement. `_prune()`, which runs at the top of every `check()`, trimmed the `_attempts` deque
behind the attempt and notional caps and did not touch the position dicts at all.

The module was not naive about this. `warm_start()` rebuilt the gate from the last 24 hours of
`orders_fired`, and the assumption written into its own docstring, *every market this bot trades is
an hourly strike, so anything older than a day is settled*, is true. `warm_start()` runs once, at
process boot. On a supervised process that restarts most days the 24-hour filter is applied often
enough to look like a rolling window. On a 72-hour soak that never restarts, it is applied exactly
once and then silently stops being true.

## What tripped it

301 real fills totalling 10,005 gross contracts landed inside a single 65-minute window at the very
start of the soak, 2026-08-21 16:47:30 to 17:52:48 ET. The `majors` correlation group caps at
10,000 contracts. The fill that crossed 10,000 tripped the cap, and because the total could only
rise, the cap never released.

`orders_fired` recorded what that cost: 4,233 real fires rejected in `majors` over the following 23
hours, effectively the whole soak, while `GET /portfolio/positions` showed nothing open the entire
time.

## Root cause

Read directly out of `execution/risk.py`: the two accumulating dicts, the `record_fill()` that only
adds, and the `_prune()` that skips them.

The gate does no I/O by design. `check()` and the `record_*` methods are pure arithmetic over
in-memory state, because the one thing that must never happen at fire time is a blocking call
between a decision and an order. That constraint is correct and is not the bug. It is why the gate
had no way to notice a position had closed. It had never been given a path to compare itself
against exchange truth, so it could only ever extrapolate from its own history.

## Fix, stage 1: make positions age out the way attempts already do

`record_fill()` now takes `now_ms` and appends to a `_fills` deque, timestamped exactly as
`_attempts` already was. `_prune()` expires fills past the rolling 24-hour window and subtracts them
back out of the position dicts. `warm_start()` populates `_fills` as well, so a restarted process
keeps pruning.

Two regression tests, one per cap:
`test_position_cap_ages_fills_out_of_the_24h_window` and
`test_correlation_group_position_cap_ages_fills_out_of_the_24h_window`.

This was shipped, and it was still wrong.

## Fix, stage 2: stop guessing, and read the exchange

A 24-hour window is a proxy for "has this settled yet", and a bad one on contracts that settle in
about an hour. It replaced an unbounded error with a bounded one. It did not replace an assumption
with a fact.

`RiskGate` gained `reconcile_open_positions()`, fed every 30 seconds from
`execution/account_monitor.py`'s existing `/portfolio/positions` poll, already running in the
executor for the dashboard, so this added no API calls, no new failure mode, and nothing at all
near the hot path. Kalshi drops a ticker from that response the moment it settles, so *absence* is
the literal "this position is closed" signal. The time-based guess is gone.

The I/O rule survives intact. `reconcile_open_positions()` is handed data its caller already fetched
and never fetches anything itself. A fire dispatched between polls still counts immediately through
`record_fill()`, layered on the last reconciled baseline, so an order placed one second after
another is not invisible to the cap, and fills already covered by a later reconciliation are
excluded, so nothing is counted twice. Before the first reconciliation completes at boot, the checks
fall back to the stage-1 rolling window.

One semantic change, taken deliberately. `/portfolio/positions` reports `position_fp`, a net signed
count, and Kalshi does not expose per-side counts there. The old ledger tracked gross contracts on
purpose, since a bot holding 5 YES and 5 NO of one strike has 10 contracts that can be wrong, not 0.
Live reconciliation can only see the net figure, so a ticker held on both sides would now read as
smaller exposure than gross accounting reported. This strategy fires one FOK order on one side per
detected edge and does not intentionally hedge a ticker, and net exposure is arguably the more
honest thing to cap against, since a hedged position risks less real capital than its gross count
suggests. It is recorded here because it needs revisiting the moment any strategy does hold both
sides.

## Validation

Full suite clean: ruff, ruff format, `mypy --strict`, pytest at 433 tests, 8 of them new. After the
executor restart there were zero position-cap rejections, against continuous rejections in the
hours before. Shipped to the parent system's executor.

The `majors` group then began hitting its separate `max_attempts_per_correlation_group_per_day`
cap, 400 a day, saturated by the same original burst, riding the 24-hour `_attempts` window that
had been working correctly the whole time. That one was expected to drain on its own as those
attempts aged out, and did.

## A false alarm, kept in the record

Mid-incident a `tail -n 5` of `executor.log` taken just after a restart appeared to show a second
bug, a risk limit from `.env` not taking effect. It had taken effect. The log file is not rotated
on restart, so the tail was still showing pre-restart lines. Filtering `orders_fired` by timestamp
showed zero such rejections after the restart, and the configured value had been live the whole
time.

It is in the write-up because the misdiagnosis and the real one came from the same debugging pass,
and only one of them was made against the database. A log tail is a sample with no stated boundary,
which makes it the wrong instrument for the question "is this still happening".

## What generalised

- A counter with no decrement is a leak. Every accumulating structure in a long-lived process needs
  the thing that removes from it identified at the point it is written.
- An assumption that holds at boot is not a rolling guarantee. `warm_start()`'s 24-hour filter was
  correct and was applied once. The bug was the gap between how often it ran and how often it
  needed to be true.
- Prefer the authoritative signal to the proxy for it, when it is already on the wire. The
  exchange's own position list was being polled thirty seconds apart for a dashboard while the risk
  gate extrapolated. The fix cost one function and no requests.
- A control that fails closed still fails. It is the cheaper direction to fail in, and it is not
  free.

Related: `docs/GUIDE.md` §7.3 (the risk controls and the mechanism behind each) and §6 (the failure
modes this project hit, of which this is one instance of a general form).
