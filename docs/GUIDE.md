# The Guide

All measured numbers live here. Anything cited elsewhere in this repository points back to this
file, so that a figure has exactly one home and cannot drift into two contradictory copies.

A note on scope, read this first. This repository is an *extraction* from a larger private system,
and the extraction is deliberate. The infrastructure, the matcher, and the record of what did not
work are here. The pricing layer, the live experiments, and the venue-facing research notes are
not. Docstrings throughout the code occasionally reference modules that were not extracted
(`decision/`, `ingest/`, `dashboard/`, `account_monitor`, `scripts/run_executor.py`) and sections
of the parent project's `ENGINEERING.md` and numbered §2 entries of the parent guide that are not
reproduced here. Those references are left exactly as written. §7.1 says what is here and what is
not.

---

## 1. What this is

Trading infrastructure for [Kalshi](https://kalshi.com) and [Polymarket US](https://polymarket.us)
prediction markets, plus the cross-venue matcher built to decide whether two markets on different
exchanges are the same tradeable claim. Python 3.12, `asyncio`/`uvloop`, `aiohttp`, SQLite.

Three facts about the parent system, before any number below.

No order has ever been sent to a real-money market. Both trading processes hard-refuse to start
unless `KALSHI_ENVIRONMENT=demo`, and any order path must separately clear a
`KALSHI_ALLOW_PRODUCTION_ORDERS` gate that is off. The research packages hold no signer and no key
material *by construction*, enforced by a test asserting the write-capable packages are absent from
its import graph (`tests/test_import_graph.py`, §7.3).

Most of the parent project is a record of things that did not work, measured carefully enough to be
sure. That is the finding. §4 summarises the closed searches this repository describes.

The latency figure everyone asks about is a lower bound, and it says so wherever it appears. §7.2
states what the clock starts and stops on, and what sits outside it.

---

## 2. The venues

The parent project keeps a register of every wire fact verified against a real response: field
shapes, fee coefficients, rate-limit budgets, WebSocket channel semantics. That register is not part
of this extraction, so cross-references anywhere in this repository to numbered subsections of §2
(§2.7 on fees, §2.10 on the market-data socket, §2.11b on order-placement fields, §2.12 and §2.13 on
per-environment keys and Polymarket US auth) resolve to this note. What survives extraction is the
discipline that produced it, which is the part that transfers:

- Every field access was added only after printing a real response and reading the keys. A green
  suite proves only that the fakes agree with the author (§6).
- Absence of a capability is a claim about the API, and only the API reference or a real response
  can support it. A 404 on a hand-built URL is evidence about the URL.
- Where a value is read from the wire, it is read at the moment it matters, because a cached copy
  from a schedule PDF may be stale. `crossvenue/fees.py` reads fee coefficients per market and
  raises on an unimplemented fee shape.

What the vendored code depends on and states for itself: Kalshi's taker fee is quadratic in price,
`0.07 × multiplier × C × P × (1 − P)`, rounded up to the cent on the whole fill, which is 1.75¢ per
contract at mid. Polymarket US charges the same shape with a `feeCoefficient` observed at `0.06`.
Polymarket US is taker-only; Kalshi is taker-only except on the series whose `fee_type`
carries a maker multiplier. `crossvenue/fees.py` has the census, the count, and the derivations.

---

## 4. What was searched, and what killed each idea

The parent project runs each search as a pre-registered experiment: criterion, sample size,
stopping rule and analysis order fixed in writing before any data, one declared extension, no
interim looks (§5). Every search it has closed has returned negative.

The searches below are the closed ones this repository describes. Others are omitted because the
work continues. An open thesis is not improved by being published, and a summary of one would be
the only part of this document that could not be checked against the code here.

### 4.1 The directional crypto strategy, falsified

The original premise. Built properly, validated offline, then falsified out of sample: net negative
in every configuration. It was left falsified and never re-cut until it passed, and `config.py`
still refuses to point the order path at production partly on the strength of it.

### 4.2 Cross-venue arbitrage, Kalshi × Polymarket US, does not exist

The search the matcher in this repository was built for. A full cross of 95,206 × 10,656 markets,
about 1.01 billion pairs, narrowed to 63,424 scored candidates and priced in 113 seconds.

Final result: 0 verified arbitrage pairs, matching the fee arithmetic that predicted none. Eighteen
apparent edges worth up to 63¢ a contract were matching artifacts the settlement gate caught, among
them a match winner priced against a maps total, an MVP market against an MVP-finalists market, and
a threshold against a rank.

The case that justifies the whole design is the one the gate initially missed. An earlier scan
passed 33 pairs as `IDENTICAL`, and all 33 were Korean KBO or Japanese NPB baseball. Both leagues
end a regular-season game in a tie once extra innings are exhausted, and Kalshi words each side as
"if {team} wins, resolves Yes", so a tie resolves NO on both legs and the "hedge" pays zero twice.
Neither venue writes the word "draw" anywhere in a baseball game's rules, so no wording-based check
could have seen it. Whether a competition can draw is a fact about the sport. The fix is an
explicitly known-incomplete allowlist, kept as one because being wrong in that direction costs a
missed match and being wrong in the other costs the position.

### 4.3 Structural arbitrage within Kalshi, does not exist

Whether Kalshi's own markets price coherently against each other. Exactly zero structural
inconsistencies across 100,859 markets. A later pass closed this section's two stated blind spots,
exhaustive fields whose rules prose forces exactly one YES, and temporal `by D1 ≤ by D2` ladders,
and returned zero on both. Field closure turned out to be absent from the payload entirely (0 of
5,625 events state it), and where prose does close a field the legs price ≥106¢.

### 4.14 Parlay coherence, real, uniformly signed, and unshortable

Kalshi's multivariate parlays priced against the product of their legs, over 1,177,933 open parlays
swept to exhaustion: 0 of 1,220 priced cheaper than the independence product, median divergence
+2.06 points. The mispricing is real and consistently in the direction a retail account cannot
sell. A mispricing you cannot take the other side of is not a trade.

### 4.12 The calibration map, real, and not a trade

Where on (category × horizon × price) is the exchange worst calibrated? A ±4-6 point tilt runs
through the whole middle of a contract's life and is gone in the final hour. Every band of it is
net-negative after fees, with the two most extreme cells missing break-even by 0.02¢ and 0.10¢. Two
cells clear the fee hurdle and are disqualified on liquidity.

### The pattern across all of them

Kalshi's taker fee is larger than the dislocations its market makes. `0.07 × p(1−p)` is 1.75¢ at
mid, the gross cross-venue dislocation measured 0-1¢, and the structural inconsistency count was
zero. The fee sits *above* the size of the errors the market makes, which kills every fee-paying
strategy built from public data before it is coded. That is one finding, arrived at five separate
ways, and it is worth more than any of the individual nulls.

---

## 5. Method

The practices that separated the one real-looking finding from the several convincing fakes.

- Pre-registration. Criterion, sample size, stopping rule and analysis order fixed in writing before
  any data, one declared extension, no interim looks. Any change to a clause, the sampling frame, or
  the sampling *procedure* bumps the rule version and restarts the count. Versions are never pooled,
  and the label-to-parameters binding is enforced by code at analysis time (§6 records what happened
  when it was not).
- Held-out discipline. Every claim that mattered was re-tested on data the analysis that produced
  it had never seen. The two meanings of "held out", new instances versus entirely absent groups,
  are stated wherever cited, because using them interchangeably cost an afternoon.
- Breadth over pooling, with the right denominator. A pooled point estimate over heterogeneous
  units is not evidence, and neither is equal-weight voting across units where the phenomenon
  cannot exist. Weight the vote by where the effect could live.
- Calibration over return for small-n verdicts. A loss *rate* has no payout leverage, and a mean at
  n=150 is hostage to two results.
- Power computed before collection, on both sides of every threshold. A criterion bug that read a
  better-than-claimed result as KILL survived precisely because the power table only priced truths
  at or below the claim.
- Guardrail parameters are set from the support. A cap set to the held-out sample's *maximum* is
  reading the support. A cap set to the value that scores best is tuning.
- Replay has a budget. The historical dataset was cut six times, each cut pre-declared or recorded
  as a degree of freedom, and a seventh was banned. No further slicing substitutes for forward
  data.
- Measure the quantity in the flag, over the window the flag names, on the population the flag is
  about. The one flag retired by an *adjacent* measurement became the costliest bug in the live
  experiment (§6).

---

## 6. Failure modes this project actually hit

The transferable output. Each entry names the general form, then the instance.

A green test suite is blind to wire-shape truth. Fakes encode what the author believed. 169, then
260, then 604 tests were green while a ladder parser raised `KeyError` on every live call, while
the order path sent `1 − yes_bid` to the wire, and while a skew metric differenced a monotonic
clock against wall time (every stored value wrong by decades, both stamps individually plausible,
nothing asserting a relation). Corollaries: print a real response before shaping a fixture, a
timestamp crossing a module boundary needs its epoch in its name or type, and a smoke test verifies
only what it can actually observe, so an empty-book order smoke test kills a correct price and an
inverted one identically.

A counter with no decrement is a leak, and an assumption that holds at boot is not a rolling
invariant. A position cap summed every fill into a per-group total and never subtracted on close,
cancel, expiry or settlement. A once-only 24-hour filter at warm start papered over it for any
process that restarted daily, and a long soak has no such reset. 301 fills totalling 10,005
contracts inside one 65-minute window locked a correlation group's cap for the following 23 hours,
rejecting 4,233 valid orders against zero open positions. The first fix, aging entries out on a
rolling window, shipped and was still wrong. It replaced an unbounded error with a bounded guess
where it should have replaced a guess with a fact. The second reads live exchange positions off a
poll already running for another consumer, at the cost of no new API calls and one documented
net-versus-gross tradeoff. Full postmortem:
[`incidents/2026-08-22-fill-ledger.md`](incidents/2026-08-22-fill-ledger.md).

A structural guarantee written only in prose decays silently, and the list of guarantees is itself
a claim. A documented invariant ("both trading processes refuse to start outside the demo
environment") was not in the code at all for an unknown period. The executor checked a different,
weaker condition. It is documented again now only because there is a named function and a test that
fails without it. A guarantee is the mechanism, and never the sentence about it.

An order was placed, filled completely, and left no row of any kind. The only thing that pointed at
it was the shape of the surrounding ids. The exchange's order ids are time-ordered, and the missing
one sorted precisely between two recorded neighbours. The audit-trail hole was then closed as a
*class*, so every refusal now writes a `rejected` row and a halted system's inaction is in the
record.

A conservative placeholder is still a made-up number, and prudence does not exempt it from the
never-guess rule. A value chosen to be safe is still a value nobody measured, and it propagates
exactly like an optimistic one.

A number moved under a component built against it. A recorder was built mid-pull against a figure
that changed before the pull finished. Numbers other components consume are flags by default, so
when one moves, grep for everywhere it is baked in.

A small-sample anecdote hardens into a fact by being cited. A headline calibration figure rested on
65 markets. At n=505 it was 27 points different. It had been quoted in three places by then, and
the run that corrected it also killed the thesis it had motivated, in the same pass, at a cost of
zero requests.

A validation run that validated nothing, recorded as if it had. A recorder's "first live
validation" had been executed as a dry run, so the write path it was supposed to prove had never
executed at all. "Validated" is a claim about which lines ran.

A schema statement that is a no-op against history. `CREATE TABLE IF NOT EXISTS` cannot alter an
existing table and raises nothing when it does not. A column added to the schema file appears on
fresh databases and silently never appears on any database that already exists. Non-additive
changes go through explicit migrations keyed on `PRAGMA user_version`.

A read-only SQLite connection cannot open a WAL database whose `-shm` sidecar is missing. The
read-only reader is restricted and also unable to create the shared-memory file it needs, so a
reader that starts before any writer has ever run fails in a way that reads as corruption.

An exclusion nobody sees is a sample shrinking silently. Rows dropped for being unscoreable are a
filter on the population, and a filter not reported is a population not described.

A silent fallback that degrades the analysis. `row.get("event_ticker") or row["ticker"]` never
raises and quietly analyses a different grouping than the one intended.

Two of the three "flaky" websocket tests were racing a fire-and-forget telemetry write. Being
fire-and-forget is a property of the production design and therefore a property the tests must
accommodate explicitly. Re-running a test that fails a third of the time is a coin flip that
eventually comes up green, which is indistinguishable from having fixed it. The third test is
genuinely timing-sensitive and is marked as such.

A version label not bound to its parameters. Two collector runs stamped the same rule version while
running different parameters. A label is only a label, and the binding has to be enforced where the
analysis reads it.

A classification that lives only in prose is applied differently by every reader, including the same
reader twice.

Single-observation tests miss multi-observation bugs. A defect that requires two records to
manifest survived four versions of a test suite that only ever wrote one.

---

## 7. The system

Module boundaries, the process model, and which guarantees are *structural* (enforced by a
mechanism) as opposed to conventional (enforced by remembering).

### 7.1 Modules

`config` → `auth` → `transport` → `execution`, `telemetry` written to by everything, `ipc` joining
the two runtime processes, `runtime` a leaf, and `crossvenue` a self-contained read-only research
package.

| Module | Owns |
|---|---|
| `config` | Typed configuration from environment variables ([configuration.md](configuration.md)). |
| `auth` | RSA-PSS request signing only, no HTTP knowledge. Hand-written, because the vendor SDK is synchronous in places and pulls a broad dependency surface, and signing is on the hot path. |
| `transport` | Signed REST + WS clients, client-side rate limiting (the exchange's 429s carry no `Retry-After`), retry with fresh signatures. Order-path timeouts are never retried, because a timed-out order may be resting. Error bodies are read before raising, and a blank 404 once cost three phases of misdiagnosis. |
| `execution` | Turns a decision into an order and dispatches it. Fractional-Kelly sizing under a hard cap, and the risk gate. |
| `ipc` | The wake channel: Unix domain socket, 4-byte big-endian length-prefixed `orjson` frames, no delimiter scanning. Wakes are fire-and-forget and deliberately lossy, because a stale fire is worse than a late one. |
| `telemetry` | SQLite via one background writer thread per process, fed by a queue. `record_*` is `put_nowait` and return. WAL, `synchronous=NORMAL`. Additive schema via `CREATE TABLE IF NOT EXISTS`, non-additive via `migrations.py` on `PRAGMA user_version`. |
| `runtime` | A leaf. It imports nothing else in the codebase, so anything may depend on it: monotonic clock plus `clock_domain()`, `flock` single-instance lock, file-presence kill switch, queue-based non-blocking logging. |
| `crossvenue` | Read-only venue research: the normalizer, the matcher, the settlement gate, the fee models. Holds no signer, by construction (§7.3). |

Not extracted, and named here so their absence is legible: the per-asset pricing pipeline
(`decision/`), market-data ingest (`ingest/`, `wsfeed/`), the fill feed, the read-only dashboards
and their React frontend, the venue HTTP adapters and scanners, the book and tape recorders, the
paper-trading lab, and the process entry points under `scripts/`. Docstrings here reference several
of them. See the scope note at the top of this file.

### 7.2 The process model

Two processes. The poller decides what to fire on. The executor owns how the order goes out, so
contract count, time in force, and self-trade prevention are all executor configuration. The split
exists so that unbounded decision work never contends with the event loop that submits an order.
The executor is the server, the long-lived, pre-warmed process that should already be listening.
Either process can restart and reconnect.

Both are bound to one host, with a shared socket, shared SQLite, and monotonic timestamps comparable
only within one machine and one boot. Splitting them across hosts means replacing the IPC layer, and
the wake carries its clock domain so a mismatch records *no* timing.

Failure behaviour: fail loudly. A malformed event or a dropped connection is retried or skipped. A
dead leg kills the process for the supervisor to restart.

Latency measurement. `detect_fire` is recorded as one span. The gaps *between* stages (queue waits,
event-loop scheduling, the socket hop) are most of the elapsed time on a healthy run, and summing
sub-measurements would make them disappear. Nothing that can raise, block, or log for a telemetry
reason may sit between building an order and sending it, and measurements on that stretch are held
in locals and written after dispatch resolves.

Measured in the parent system over its whole life (2026-08-19 to 2026-08-29), on the demo venue:
detect→fire p50 10.23ms / p90 29.66ms / p99 46.61ms / max 898.82ms on n=734 real order fires,
against a <15ms budget, so the budget was met at the median and missed by roughly 3× at p99.
Signing dominated: p50 3.68ms / p99 9.02ms on real fires against its own 3ms budget, while the
isolated warm-loop microbenchmark on that host read 1.65ms (that host's run is not in
`../benchmarks/history.csv`, whose committed runs on other machines read p50 0.33ms to 0.93ms), an
in-situ cost about 2.2× the benchmark. Shadow fires (n=92,441; identical path, real signature,
stopped before the socket write) ran p50 6.72ms / p99 13.49ms, faster than real fires at both
points; the gap was never explained. The `sign,yes` row of the per-stage table below puts signing
on a shadow fire at p50 5.50ms / p99 11.48ms on that same n=92,441, above the real-fire signature
at both points, so whatever made shadow fires quicker overall was not the signature. Round trip to
the venue (`dispatch_ack`) ran p50 123ms / p99 487ms, an order of magnitude above everything local.
The per-stage table is committed as
[`../benchmarks/parent_latency_summary.csv`](../benchmarks/parent_latency_summary.csv).

A correction, kept on the record. From 2026-08-21 to 2026-08-29 this section quoted p50 4.33ms /
p99 10.53ms from a single n=139 window. That window was favourable, and the number was cited until
it read as a fact. The lifetime distribution above replaces it. All figures are lower bounds: the
clock starts after `orjson.loads()` and stops when the request reaches `aiohttp`, so network
transit, TLS, and exchange-side processing are all outside them.

That live figure is not reproducible from this repository, because it needs a signed round trip to
a demo account. What *is* reproducible here is the pair of contributors measurable without a
network call, and `benchmarks/latency_bench.py` reports their sum as an explicitly *partial*
estimate. See [`../README.md`](../README.md) for the benchmark and its committed history.

### 7.3 Structural guarantees, and the mechanism behind each

- `crossvenue` cannot place an order. It holds no signer, no key material, and does not import
  `execution`, so the type that can reach the order endpoint is not in scope, and
  `tests/test_import_graph.py` fails if any of `execution`, `auth` or `transport` ever appears in
  its import graph. The authenticated, five-times-higher rate-limit tier was considered for the
  recorders and rejected on exactly this ground: a 5× budget does not buy the loss of the one
  mechanism that makes the package unable to trade.
- `transport` must not import `telemetry`. Timings only transport can see are returned via a
  mutated out-parameter (`RequestTimings`), and `execution` owns the write. Pinned by
  `tests/test_import_graph.py`.
- Execution legality is in the type system. `crossvenue/venues.py` carries a `VenuePolicy` per
  venue and `assert_us_executable()` is the greppable chokepoint any future order path must pass.
  Polymarket International is closed to US persons, so it is reference data only and never a leg.
  Whether a venue is executable is a *column* in the research dataset, and never a write-time
  filter.
- An order cannot be dispatched without the permission guard. `OrderDispatcher.__init__` takes a
  required, defaultless `assert_orders_permitted` callable, and `dispatch()` calls it before the
  client order id is minted, before signing, before any telemetry, so a refused dispatch leaves no
  request, no consumed id, and no `orders_fired` row. The type cannot be constructed without a
  guard, which moves the rule from "every author remembers" to "the type checker asks". `execution`
  takes a *callable*, so the rule keeps one home without the package gaining a dependency on the
  config loader.
- Risk controls sit at the one chokepoint that still works when the poller is wedged, the executor,
  immediately before dispatch. Attempt, notional and concurrency caps, per-ticker and
  correlation-group position caps reconciled against live exchange positions, a restart-surviving
  re-fire cooldown, a `uuid4` client order id unique-constrained in SQLite, a `flock`
  single-instance lock, and a file-based kill switch that works from any shell against a wedged
  process. Every refusal writes a `rejected` row.
- `runtime` is a leaf. It imports nothing else in the codebase, which is what makes it safe for
  everything else to depend on, and `tests/test_import_graph.py` fails if that ever changes.
