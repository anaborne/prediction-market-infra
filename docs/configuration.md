# Configuration

All runtime configuration comes from environment variables, loaded into a frozen
`KalshiBotConfig` dataclass by `load_config()` in [`src/kalshi_bot/config.py`](../src/kalshi_bot/config.py).

Each entry point calls `load_dotenv()` before `load_config()`, so a `.env` file in the repository
root is picked up automatically. `uv run` does not load `.env` on its own, and the explicit
`load_dotenv()` call is what makes it work. Copy [`.env.example`](../.env.example) to `.env` to
start.

## Credentials

| Variable | Default | Required | Notes |
|---|---|---|---|
| `KALSHI_API_KEY_ID` | `""` | yes | API key identifier from the Kalshi demo web UI. Both trading processes refuse to start if empty. |
| `KALSHI_PRIVATE_KEY_PATH` | `""` | yes | Path to the PEM-encoded RSA private key. Read once, eagerly, at signer construction. Encrypted keys are not supported. `chmod 600` it. |
| `POLYMARKET_US_KEY_ID` | `""` | not yet | Key ID from `polymarket.us/developer`. Not read by any code yet, see below. |
| `POLYMARKET_US_SECRET_KEY` | `""` | not yet | Ed25519 secret, base64. Shown once at creation. Not read by any code yet. |

### Polymarket US credentials

Obtained at [polymarket.us/developer](https://polymarket.us/developer) after completing KYC in the
Polymarket US iOS app, signing in with the same method used in the app.

These are declared and not yet consumed. `config.py` does not read them and no client uses them. The
*public* adapter (`crossvenue/polymarket_us_public.py`, built 2026-08-22, not part of this
extraction) deliberately needs none of this. It reads `gateway.polymarket.us`, which is
unauthenticated, and holds no key material at all, which is what keeps `crossvenue` read-only by
construction. These variables are consumed by the Ed25519 signer, which is what streaming and order
placement need. The names are fixed here first so that signer is built against a settled contract.

What each is actually needed for:

| | Host | Needs a key? |
|---|---|---|
| Market data, order books, BBO, settlement | `gateway.polymarket.us` | No, fully public |
| Placing orders, portfolio, balances | `api.polymarket.us` | Yes |
| Both WebSocket streams | `api.polymarket.us` | Yes |

So the read-only scanning work is unblocked without any credential at all, and the key is what
unlocks streaming and execution. Auth is Ed25519 over `timestamp + METHOD + path`, base64-encoded,
headers `X-PM-Access-Key` / `X-PM-Timestamp` / `X-PM-Signature`, with a 30-second timestamp window.
See [GUIDE.md](GUIDE.md) §2.13 for the verified details, and note it is a different algorithm from
Kalshi's RSA-PSS despite the identical message shape.

> The secret is displayed once. If it is lost, revoke the key in the developer portal and issue a
> new one, since there is no way to retrieve it. Keep it in `.env` (gitignored) and nowhere else:
> not in a commit, not in a shell history line, not pasted into a message window.

## Endpoints

REST and WebSocket use different hosts, `external-api` vs `external-api-ws`. This is not a
derivation you get for free, which is why both are configured explicitly.

These are Kalshi's Predictions product (event contracts), which is the only one this bot uses.
Kalshi's separate Perps product lives under `/trade-api/v2/margin/` on the same host, with its own
WebSocket host, and holds a separate balance that does not back event-contract orders. There is no
configuration here for it, deliberately.

| Variable | Default | Required | Notes |
|---|---|---|---|
| `KALSHI_DEMO_BASE_URL` | `""` | yes | `https://external-api.demo.kalshi.co` |
| `KALSHI_DEMO_WS_URL` | `""` | yes | `wss://external-api-ws.demo.kalshi.co` |
| `KALSHI_PROD_BASE_URL` | `""` | no | Loaded but currently unused, see the warning below. |
| `KALSHI_PROD_WS_URL` | `""` | no | Loaded but currently unused. |

> ⚠️ The production URLs are selected and not yet exercised. `KalshiBotConfig.base_url` and
> `.ws_url` return the `prod` pair when `KALSHI_ENVIRONMENT=prod`, but the two trading processes
> refuse to start outside `demo` (`assert_demo_trading_process()`), so nothing in the parent
> system has ever connected to them. The entry points that would consume these live in the
> parent's `scripts/`, which are not part of this extraction, and whether every one of them reads
> the URL through `base_url` instead of `demo_base_url` cannot be verified from this repository.

## Environment selection

| Variable | Default | Required | Notes |
|---|---|---|---|
| `KALSHI_ENVIRONMENT` | `"demo"` | no | Must be exactly `demo` or `prod`. `load_config()` raises `ValueError` otherwise. `scripts/run_decision_engine.py` and `scripts/run_executor.py` both exit non-zero unless it is `demo`. |

This is the highest-stakes field in the file, so it is one of only four that `load_config()`
validates at all (with the two order-execution enums and the shard allowlist).

## Telemetry and IPC

| Variable | Default | Required | Notes |
|---|---|---|---|
| `KALSHI_TELEMETRY_DB_PATH` | `""` | yes | SQLite file. See the empty-value warning below. |
| `KALSHI_IPC_SOCKET_PATH` | `./data/executor.sock` | no | Unix domain socket the executor binds and the poller connects to. Both processes must resolve it to the same path. |

> ⚠️ An empty `KALSHI_TELEMETRY_DB_PATH` fails silently. It becomes `Path("")`, and
> `sqlite3.connect("")` opens a temporary on-disk database that is deleted when the connection
> closes. The bot will appear to run normally and record nothing. Always set it explicitly.

## Order execution

These control how a real fire executes. All are read by the executor.

| Variable | Default | Required | Notes |
|---|---|---|---|
| `KALSHI_FIXED_ORDER_CONTRACT_COUNT` | `1` | no | Contracts per *shadow* fire, and the floor under a real fire's Kelly-sized count (see "Position sizing" below). Parsed with an unguarded `int()`, so a non-numeric value is a startup traceback. |
| `KALSHI_ORDER_TIME_IN_FORCE` | `fill_or_kill` | no | One of `fill_or_kill`, `good_till_canceled`, `immediate_or_cancel`. Validated. |
| `KALSHI_ORDER_SELF_TRADE_PREVENTION_TYPE` | `taker_at_cross` | no | One of `taker_at_cross`, `maker`. Validated. |
| `KALSHI_ENVIRONMENT` | `demo` | yes | `demo` or `prod`. Selects both the base URLs and the credentials. Kalshi API keys are environment-specific, and each returns `401` against the other environment ([GUIDE.md](GUIDE.md) §2.12), so this one variable has to move both. |
| `KALSHI_ALLOW_PRODUCTION_ORDERS` | `false` | no | Permits real-money orders when the environment is `prod`. A separate switch on purpose, because reading production data is ordinary and trading real money on it is not, so selecting the production environment is never by itself consent to trade there. Checked by `config.assert_orders_permitted()`, the greppable chokepoint every order path calls. Anything not plainly affirmative (`true`/`1`/`yes`) fails closed. The dashboard's environment button cannot set this, and it is a deliberate file edit. |
| `KALSHI_PROD_API_KEY_ID` / `KALSHI_PROD_PRIVATE_KEY_PATH` | unset | for prod | Production credentials. Used when `KALSHI_ENVIRONMENT=prod`. |
| `KALSHI_DEMO_API_KEY_ID` / `KALSHI_DEMO_PRIVATE_KEY_PATH` | unset | for demo | Demo credentials. Used when `KALSHI_ENVIRONMENT=demo`. This is where a demo key goes. Generate one at `kalshi.com/account/profile` while logged into the demo site. The private key is shown once and never again. |
| `KALSHI_API_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH` | unset | fallback | Shared credentials, used only when the environment-specific pair above is unset. Kept so single-key setups keep working. |
| `KALSHI_ALLOWED_EXCHANGE_INDEXES` | `""` (empty) | yes, for the executor | Comma-separated demo shards real orders may route to, e.g. `2`. Validated as integers. At startup the executor compares it against the shards the account actually holds balance on and refuses to start when it is empty or names an unfunded shard ([GUIDE.md](GUIDE.md) §2.12). Empty means "not configured", never "no restriction". |
| `KALSHI_KILLSWITCH_PATH` | `./data/killswitch` | no | File whose existence halts new real dispatch at the executor. Operate it with `scripts/killswitch.py`. The executor and the script must resolve the same path (run from the repo root, or use an absolute path). |
| `KALSHI_LOG_DIR` | `./data/logs` | no | Directory the entry-point scripts write rotating log files into (32 MB × 5 backups per process, via `runtime/logging_setup.py`). |

> ⚠️ Changing `KALSHI_ORDER_TIME_IN_FORCE` away from `fill_or_kill` removes a real safety
> property. Under FOK, an order either fills immediately or is killed, and nothing rests. Under
> `good_till_canceled`, orders rest on the book and this codebase has no mechanism to track,
> reconcile, or cancel them.

## Not configurable by environment variable

Some tunables that matter operationally live in `decision.runner.RunnerConfig` (in the parent
system's `decision/` package, which is not part of this extraction), a dataclass with defaults, and
nothing reads them from the environment. Changing one means editing the default or constructing a
`RunnerConfig` in `scripts/run_decision_engine.py`, which currently does not.

| Field | Default | What it does |
|---|---|---|
| `watch_width` | `10` | Strikes tracked on each side of spot |
| `safety_margin` | `0.01` | Extra required edge over Kalshi's fee, in dollars per contract |
| `refire_cooldown_seconds` | `30.0` | Minimum re-fire interval per (ticker, direction) |
| `strike_ladder_interval_seconds` | `60.0` | How often each asset re-fetches its ladder |
| `queue_maxsize` | `1000` | Bound on each asset's inbound event queue |
| `decision_result_sample_every` | `20` | Record 1-in-N non-firing decisions |
| `shadow_fire_every` | `250` | Send a dry-run wake for 1-in-N non-firing decisions. `0` disables |

`shadow_fire_every` is the one most likely to need tuning during a long run. It sets the sample
rate for the detect→fire latency measurement, and it is the only configurable source of the
telemetry write volume added by that measurement. Lowering it costs one RSA signature per shadow
fire (~3 ms of CPU, measured), and that signature is synchronous, so it does not yield to a real
fire arriving behind it. See [GUIDE.md](GUIDE.md).

## Risk gate

`KALSHI_RISK_GATE_ENABLED` (default `true`) is the on/off switch for the whole gate.
`scripts/run_executor.py` builds a `RiskGate` unless this is explicitly falsy. When it is, it
passes `risk_gate=None` to `ExecutorServer`, which its docstring documents as disabling the check
outright, so no attempt, notional, position, concurrency, or cooldown cap blocks a real fire.

It defaulted to `false` through the demo-trading phase, on the argument that a strategy meant to
fire on every statistically-grounded edge it finds shouldn't stop at a fixed trade-count or
notional budget. That default is now `true`, because the old one made the *omission* of a variable
the thing that removed every cap, which inverts the rule the limits below follow ("unset means
safe, not unlimited") and traps an operator who tunes every `KALSHI_RISK_*` value carefully and
still ends up with no gate. Turning every cap off is still possible and still one line. It just has
to be written down on purpose.

`execution.risk.RiskLimits` caps are read by the executor from the environment. Unset means the
default column below, the same numbers `RiskLimits`' own dataclass defaults carry, chosen as
conservative values appropriate for an account that could hold real money. Widen these only for a
low-stakes paper account where you deliberately want to see more fills. Before ever pointing this
configuration at a funded real-money account, confirm every one of these is back to something this
tight (or tighter).

| Variable | Default | `RiskLimits` field | What it caps |
|---|---|---|---|
| `KALSHI_RISK_MAX_ATTEMPTS_PER_HOUR` | `10` | `max_attempts_per_hour` | Wire attempts (dispatches, not rejections), rolling hour |
| `KALSHI_RISK_MAX_ATTEMPTS_PER_DAY` | `50` | `max_attempts_per_day` | Wire attempts, rolling 24 h |
| `KALSHI_RISK_MAX_ATTEMPTS_PER_TICKER_PER_DAY` | `5` | `max_attempts_per_ticker_per_day` | Wire attempts per market ticker, rolling 24 h |
| `KALSHI_RISK_MAX_NOTIONAL_PER_DAY_DOLLARS` | `50` | `max_notional_per_day_dollars` | Sum of count × price dispatched, rolling 24 h |
| `KALSHI_RISK_MAX_CONCURRENT_DISPATCHES` | `2` | `max_concurrent_dispatches` | Dispatches in flight at once |
| `KALSHI_RISK_MAX_POSITION_CONTRACTS_PER_TICKER` | `5` | `max_position_contracts_per_ticker` | Open contracts on one ticker right now (live-reconciled, see below) |
| `KALSHI_RISK_MAX_ATTEMPTS_PER_CORRELATION_GROUP_PER_DAY` | `10` | `max_attempts_per_correlation_group_per_day` | Wire attempts across every ticker sharing an `AssetConfig.correlation_group`, rolling 24 h |
| `KALSHI_RISK_MAX_POSITION_CONTRACTS_PER_CORRELATION_GROUP` | `10` | `max_position_contracts_per_correlation_group` | Open contracts across every ticker sharing a correlation group right now (live-reconciled) |
| `KALSHI_RISK_MAX_ATTEMPTS_PER_EVENT_PER_DAY` | `6` | `max_attempts_per_event_per_day` | Wire attempts across every strike of one event, rolling 24 h |
| `KALSHI_RISK_MAX_POSITION_CONTRACTS_PER_EVENT` | `6` | `max_position_contracts_per_event` | Open contracts across every strike of one event right now (live-reconciled) |
| `KALSHI_RISK_REFIRE_COOLDOWN_SECONDS` | `60` | `refire_cooldown_seconds` | Same (ticker, direction) re-fire spacing, survives restart |

The two per-event variables are newer than the rest, added for caps `RiskGate` was already
enforcing. They had no configuration surface and `scripts/run_executor.py` never passed them, so
they silently took `RiskLimits`' dataclass defaults and no operator could move them.
`tests/test_config.py` now reads `RiskLimits`' own field list and fails if any cap lacks a
counterpart here, so the next one added cannot go unwired the same way.

`allowed_exchange_indexes` is the one `RiskLimits` field that stays wired from
`KALSHI_ALLOWED_EXCHANGE_INDEXES` above. It is a correctness gate (the shard gate) and no kind of
throttle, so there is no "wider" setting for it that makes sense.

Interaction warning: these eleven caps aren't independent. Raising `max_attempts_per_hour` alone
just moves the bottleneck to `max_attempts_per_day`, then to `max_attempts_per_ticker_per_day` on
whichever strike is trading most (fire demand concentrates hard on a handful of near-the-money
strikes), then to the notional cap. Change them together, proportional to observed demand.
`scripts/soak_report.py`'s "Distinct order errors" section shows which cap is currently binding.

The two correlation-group caps sit alongside the per-ticker ones and never replace them. A ticker
belongs to at most one correlation group (`decision/asset_registry.py`'s `AssetConfig`), and a fire
is checked against both its ticker's caps and its group's, whichever binds first rejects. Two
tickers can never collide on the per-ticker caps just because they share a group (they are
different strings), so the group caps are the only thing bounding combined exposure across, for
example, an hourly series and its 15-minute counterpart on the same underlying.

The two position caps track live Kalshi state and never a fill count. `execution.account_monitor`'s
existing 30-second `GET /portfolio/positions` poll reconciles `RiskGate`'s position ledger against
what's actually open on the account, so a ticker Kalshi has removed from that response (settled) no
longer counts toward either cap, typically within 30 seconds of settling and never up to a day
later. A dispatch's fill still counts immediately, before the next poll confirms it. See
[GUIDE.md](GUIDE.md), including the tradeoff when tuning these: the live figure is
Kalshi's net position per ticker (`position_fp`), so a ticker deliberately held on both sides at
once would read as smaller than the older gross accounting reported.

## Position sizing

`execution.position_sizing.size_position()` replaces `KALSHI_FIXED_ORDER_CONTRACT_COUNT` as the
contract count for a real fire. That variable is still used (as the size of every *shadow* fire,
and as the floor under a real fire's sized count), and a real fire is no longer always that one
number. It stakes a fraction of the [Kelly
criterion](https://en.wikipedia.org/wiki/Kelly_criterion) for the bet the EV gate already decided
to take: `f* = edge / (1 - kalshi_price)`, both already computed by the decision engine and carried
on the wake (`WakeMessage.edge`/`kalshi_price`), so no new wire field was needed.

| Variable | Default | Field | What it controls |
|---|---|---|---|
| `KALSHI_KELLY_FRACTION` | `0.15` | `ExecutorServer.kelly_fraction` | Fraction of full Kelly (`f*`) actually staked. `0.15` stakes 15% of what full Kelly computes |
| `KALSHI_MAX_POSITION_PCT_OF_BALANCE` | `0.02` | `ExecutorServer.max_position_pct_of_balance` | Hard ceiling on the staked fraction of spendable balance, applied after `KALSHI_KELLY_FRACTION` |

Balance is the sum of `GET /portfolio/balance`'s `balance_breakdown` entries for shards in
`KALSHI_ALLOWED_EXCHANGE_INDEXES`, and never the account's total, which may include shards this bot
cannot route an order to. It is refreshed by the same 30-second account poll that feeds the
dashboard (`execution.account_monitor`), cached in memory, and read (never fetched) at fire time.
Before the first poll completes, or if the poll has been failing, sizing falls back to
`KALSHI_FIXED_ORDER_CONTRACT_COUNT`.

Full Kelly (`KALSHI_KELLY_FRACTION=1.0`) is not the safe default and is not recommended while a
strategy is still being tuned. It is only optimal if `model_probability` is exactly correct.
Overconfidence a real model will have makes full Kelly stake far more than the edge actually
supports, and the formula does not know the difference. Start low (`0.1`-`0.25` is standard
"fractional Kelly" practice) and raise it as fills accumulate and settled outcomes give you
evidence the model's probabilities are calibrated in magnitude as well as in direction.

Raising either of these interacts with the risk gate above, the same way the risk gate's own caps
interact with each other. A larger `KALSHI_MAX_POSITION_PCT_OF_BALANCE` against a real balance can
size a single fire at a low tail-zone price (where this strategy's edge concentrates) into
thousands of contracts. `KALSHI_RISK_MAX_POSITION_CONTRACTS_PER_TICKER`/`_PER_CORRELATION_GROUP`
and `KALSHI_RISK_MAX_NOTIONAL_PER_DAY_DOLLARS` are what actually stop that, and this section is
not, so raise them together and do not assume a small `KALSHI_KELLY_FRACTION` alone keeps a fire
small. The executor's startup log (`Position sizing: ...`) prints what actually took effect, the
same convention as `Risk limits: ...`.

The executor prints the resolved limits on startup (`Risk limits: ...`), so `data/logs/executor.log`
is the fastest way to confirm what actually took effect after a config change.

## Relative paths and the working directory

`KALSHI_PRIVATE_KEY_PATH`, `KALSHI_TELEMETRY_DB_PATH`, and `KALSHI_IPC_SOCKET_PATH` are all
relative in `.env.example`, and `load_dotenv()` searches upward from the current working directory.
Run every process from the repository root, or all three silently resolve differently. The most
common symptom is a poller and executor that never find each other's socket, and a telemetry
database that appears empty.

## Minimal working `.env`

```dotenv
KALSHI_API_KEY_ID=your-key-id-here
KALSHI_PRIVATE_KEY_PATH=./secrets/kalshi_demo_private_key.pem

KALSHI_DEMO_BASE_URL=https://external-api.demo.kalshi.co
KALSHI_DEMO_WS_URL=wss://external-api-ws.demo.kalshi.co

KALSHI_ENVIRONMENT=demo
KALSHI_TELEMETRY_DB_PATH=./data/telemetry.sqlite
KALSHI_IPC_SOCKET_PATH=./data/executor.sock
```

Confirm secrets are not tracked before committing anything:

```bash
git check-ignore -v .env secrets/kalshi_demo_private_key.pem data/
```
