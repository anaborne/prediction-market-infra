-- kalshi_bot telemetry schema
--
-- SQLite schema for recording orders fired, market snapshots observed, and latency events
-- measured across the ingest -> decision -> execution pipeline. Timestamps are stored as
-- Unix epoch milliseconds (INTEGER) for cheap comparison and to match Kalshi's own
-- millisecond-precision timestamps used in request signing.
--
-- Prices and contract counts are stored as TEXT holding Kalshi's own fixed-point decimal string
-- format (e.g. price "0.5600", count "10.00") rather than as INTEGER cents or REAL, mirroring
-- `POST /portfolio/events/orders` and `GET /markets/{ticker}` exactly (both now dollar/count
-- fixed-point strings, not integer cents) and avoiding float rounding entirely. See
-- the order-v2 migration.

PRAGMA foreign_keys = ON;

-- One row per correlation_id in use. orders_fired / market_snapshots / latency_events all
-- reference this table so `PRAGMA foreign_keys = ON` actually enforces something instead of
-- being a dead setting; a row here is upserted (INSERT OR IGNORE) before any child row that
-- introduces a new correlation_id.
CREATE TABLE IF NOT EXISTS correlations (
    correlation_id  TEXT    PRIMARY KEY,
    created_at_ms   INTEGER NOT NULL
);

-- One row per order the bot attempted to place, via `POST /portfolio/events/orders` (Kalshi's
-- current order-creation endpoint; the legacy `POST /portfolio/orders` shape this table used to
-- mirror is being retired, no earlier than 2026-05-06 per Kalshi's docs). `outcome_side` replaces
-- the old `side`/`action` pair: the current API expresses direction as one outcome choice
-- (`yes`/`no`) at a price, not a buy/sell action layered on top, see
-- the order-v2 migration. `price_dollars` is always required
-- (Kalshi's current endpoint has no separate market-order type; immediacy is expressed via
-- `time_in_force` against an explicit price instead).
CREATE TABLE IF NOT EXISTS orders_fired (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id              TEXT    NOT NULL REFERENCES correlations (correlation_id), -- ties this order to related latency_events
    client_order_id             TEXT    NOT NULL UNIQUE,   -- bot-generated idempotency key
    kalshi_order_id             TEXT,                      -- order id assigned by Kalshi, once known
    ticker                      TEXT    NOT NULL,
    -- `AssetConfig.correlation_group` (e.g. "majors"), carried from the wake message so the risk
    -- gate's per-group caps survive a restart without this table's reader needing to import
    -- `decision` to re-derive it (`execution`/`ipc` have zero dependency on `decision` by
    -- design, see `ipc/protocol.py`). Nullable, not `NOT NULL DEFAULT ''`: a bound `NULL`
    -- bypasses a column `DEFAULT` (`_insert()`'s `row.get(column)` sends one explicitly for
    -- every existing writer that predates this field), so `NOT NULL` here would have silently
    -- dropped their rows instead of defaulting them. NULL/'' both read back as "unknown", which
    -- the risk gate treats as "skip the group cap for this row" rather than guessing a group.
    correlation_group           TEXT,
    outcome_side                TEXT    NOT NULL CHECK (outcome_side IN ('yes', 'no')),
    count                       TEXT    NOT NULL CHECK (CAST(count AS REAL) > 0),
    price_dollars               TEXT    NOT NULL CHECK (
        CAST(price_dollars AS REAL) > 0 AND CAST(price_dollars AS REAL) < 1
    ),
    time_in_force               TEXT    NOT NULL CHECK (
        time_in_force IN ('fill_or_kill', 'good_till_canceled', 'immediate_or_cancel')
    ),
    self_trade_prevention_type  TEXT    NOT NULL CHECK (
        self_trade_prevention_type IN ('taker_at_cross', 'maker')
    ),
    -- `CreateOrderV2Request.post_only` as this bot sent it (added by migration 5): 1 = the
    -- exchange was asked to cancel the order rather than let it cross and take liquidity, 0 = it
    -- was allowed to cross. Stored because the whole maker thesis turns on whether a quote paid
    -- the maker or the taker fee, and that is not recoverable from the response alone. Nullable:
    -- every pre-migration row was written before the flag was sent at all, and NULL there means
    -- "not sent", which is not the same claim as "sent false".
    post_only                   INTEGER          CHECK (post_only IN (0, 1)),
    status                      TEXT    NOT NULL CHECK (
        status IN ('pending', 'submitted', 'accepted', 'rejected', 'filled', 'canceled', 'error')
    ),
    error_message               TEXT,
    -- Fill reconciliation, from CreateOrderV2Response (added by migration 2; nullable because
    -- error rows and rows written before the migration have no response to read). For a
    -- fill_or_kill order this is the complete final state, it either fully filled or died , 
    -- captured for free from the response the dispatch already receives. All four are Kalshi's
    -- fixed-point strings verbatim, same convention as `count`/`price_dollars` above.
    fill_count                  TEXT,                      -- contracts filled immediately on placement
    remaining_count             TEXT,                      -- for IOC/FOK, final state after unfilled are canceled
    average_fill_price          TEXT,                      -- VWAP per contract; present when fill_count > 0
    average_fee_paid            TEXT,                      -- VWAP fee per contract; present when fill_count > 0
    requested_at_ms             INTEGER NOT NULL,          -- when the bot decided to place the order
    submitted_at_ms             INTEGER,                   -- when the request was sent
    acknowledged_at_ms          INTEGER,                   -- when Kalshi responded
    created_at_ms               INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER) * 1000)
);

CREATE INDEX IF NOT EXISTS idx_orders_fired_ticker ON orders_fired (ticker);
CREATE INDEX IF NOT EXISTS idx_orders_fired_status ON orders_fired (status);
CREATE INDEX IF NOT EXISTS idx_orders_fired_requested_at ON orders_fired (requested_at_ms);
CREATE INDEX IF NOT EXISTS idx_orders_fired_correlation_id ON orders_fired (correlation_id);

-- One row per observed market state snapshot (from polling or streaming ingest). Price/volume
-- columns hold Kalshi's current `GET /markets/{ticker}` fixed-point dollar/count strings
-- (`yes_bid_dollars`, `volume_fp`, etc.) directly, not the retired integer-cents fields.
CREATE TABLE IF NOT EXISTS market_snapshots (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id    TEXT    NOT NULL REFERENCES correlations (correlation_id),
    ticker            TEXT    NOT NULL,
    yes_bid_dollars   TEXT,
    yes_ask_dollars   TEXT,
    no_bid_dollars    TEXT,
    no_ask_dollars    TEXT,
    volume            TEXT,
    open_interest     TEXT,
    source            TEXT    NOT NULL CHECK (source IN ('poll', 'ws')),
    observed_at_ms    INTEGER NOT NULL,
    created_at_ms     INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER) * 1000)
);

CREATE INDEX IF NOT EXISTS idx_market_snapshots_ticker ON market_snapshots (ticker);
CREATE INDEX IF NOT EXISTS idx_market_snapshots_observed_at ON market_snapshots (observed_at_ms);
CREATE INDEX IF NOT EXISTS idx_market_snapshots_ticker_observed_at
    ON market_snapshots (ticker, observed_at_ms);

-- One row per timed stage in the ingest -> decision -> execution -> telemetry pipeline.
-- 'detect_fire' is the headline number and is not a stage in the same sense as the others: it is
-- the whole span, stamped when a WebSocket frame finishes parsing in the poller and closed when
-- the executor sends (or, for a shadow fire, would have sent) the order. It is recorded directly
-- rather than summed from the stages below, because a sum silently omits the gaps between them.
-- Its metadata_json carries {"dry_run": true|false}; see
-- the detect-to-fire measurement design for why most rows are shadow fires.
-- 'wake_send'/'wake_recv' (Phase 8) bracket the poller->executor IPC hop: 'wake_send' is written
-- by the poller process's TelemetryDB (enqueue to socket-write-complete), 'wake_recv' by the
-- executor process's own, separate TelemetryDB (socket-read-complete to ack-write-complete). Both
-- share the fire's correlation_id with that fire's decision_results/orders_fired rows even though
-- they're written by two different processes' writer threads against the same SQLite file. See
-- the poller/executor IPC design.
-- 'stage' deliberately carries no CHECK constraint. The permitted vocabulary lives in
-- `db.py::LATENCY_STAGES` and is validated synchronously in `record_latency_event()`, in the
-- calling thread, before the row is ever queued. A CHECK here would be enforced on the writer
-- thread instead, where the resulting error has no caller to reach and is logged and dropped --
-- turning a typo'd stage name into a silent hole in the latency data rather than a loud failure.
-- Existing databases are rebuilt to this shape by `migrations.py`; see
-- the detect-to-fire measurement design.
CREATE TABLE IF NOT EXISTS latency_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id  TEXT    NOT NULL REFERENCES correlations (correlation_id),
    stage           TEXT    NOT NULL,
    started_at_ms   INTEGER NOT NULL,
    ended_at_ms     INTEGER NOT NULL,
    duration_ms     REAL    NOT NULL,
    metadata_json   TEXT,                                -- optional free-form JSON context
    created_at_ms   INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER) * 1000)
);

CREATE INDEX IF NOT EXISTS idx_latency_events_stage ON latency_events (stage);
CREATE INDEX IF NOT EXISTS idx_latency_events_correlation_id ON latency_events (correlation_id);
CREATE INDEX IF NOT EXISTS idx_latency_events_started_at ON latency_events (started_at_ms);

-- One row per free-exchange price tick used to approximate a Kalshi settlement index (the
-- decision/ package's fair-value input), for audit/backtesting. Unlike orders_fired and
-- market_snapshots, prices here are REAL, not TEXT fixed-point: this data never crosses Kalshi's
-- wire format (the fixed-point-string convention exists specifically to mirror Kalshi's own
-- format and avoid float rounding on values sent back to Kalshi; exchange-feed prices never are).
-- See the directional-strategy design.
CREATE TABLE IF NOT EXISTS index_observations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id    TEXT    NOT NULL REFERENCES correlations (correlation_id),
    asset             TEXT    NOT NULL,   -- e.g. 'BTC', 'ETH', matches asset_registry.py's keys
    exchange          TEXT    NOT NULL,   -- e.g. 'coinbase', source of this tick
    price             REAL    NOT NULL,   -- observed price in USD
    fair_value_index  REAL,               -- this module's aggregated index estimate at this tick,
                                           -- once combined across exchanges (nullable: a raw
                                           -- per-exchange tick may be logged before aggregation)
    observed_at_ms    INTEGER NOT NULL,
    created_at_ms     INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER) * 1000)
);

CREATE INDEX IF NOT EXISTS idx_index_observations_asset ON index_observations (asset);
CREATE INDEX IF NOT EXISTS idx_index_observations_observed_at
    ON index_observations (observed_at_ms);
CREATE INDEX IF NOT EXISTS idx_index_observations_asset_observed_at
    ON index_observations (asset, observed_at_ms);

-- One row per decision/engine.py strike evaluation. Written while the decision engine is
-- the directional-strategy design: dispatch() is never called, so this table is the durable
-- record of what the engine would have done. should_fire=1 rows are always recorded; should_fire=0
-- rows are sampled by the caller (decision/runner.py), not recorded 100% of the time, since
-- _evaluate() runs once per active watch-set strike on every tick from either feed. Like
-- index_observations, model_probability/kalshi_price/fee/edge are REAL, not TEXT fixed-point:
-- these are the engine's own computed values and never cross Kalshi's wire format. See
-- the decision-engine runner design.
CREATE TABLE IF NOT EXISTS decision_results (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id      TEXT    NOT NULL REFERENCES correlations (correlation_id),
    market_ticker       TEXT    NOT NULL,
    asset               TEXT    NOT NULL,
    should_fire         INTEGER NOT NULL CHECK (should_fire IN (0, 1)),
    direction           TEXT    NOT NULL CHECK (direction IN ('yes', 'no')),
    model_probability   REAL    NOT NULL,
    kalshi_price        REAL    NOT NULL,
    fee                 REAL    NOT NULL,
    edge                REAL    NOT NULL,
    ts_ms               INTEGER NOT NULL,
    created_at_ms       INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER) * 1000)
);

CREATE INDEX IF NOT EXISTS idx_decision_results_ticker ON decision_results (market_ticker);
CREATE INDEX IF NOT EXISTS idx_decision_results_asset ON decision_results (asset);
CREATE INDEX IF NOT EXISTS idx_decision_results_should_fire ON decision_results (should_fire);
CREATE INDEX IF NOT EXISTS idx_decision_results_ts ON decision_results (ts_ms);

-- One row per live process, upserted (INSERT OR REPLACE) on every beat, current liveness state,
-- not history, which is why there is no autoincrement id and no created_at_ms. The dashboard's
-- /health derives ok/degraded/down from beat_at_ms staleness, and queue_depth/dropped_rows carry
-- each process's own TelemetryDB writer-queue state, the only way another process can see them,
-- since they are in-memory counters. See the fail-loudly TaskGroup rule.
CREATE TABLE IF NOT EXISTS heartbeats (
    process        TEXT    PRIMARY KEY,  -- 'poller' | 'executor'
    pid            INTEGER NOT NULL,
    started_at_ms  INTEGER NOT NULL,     -- when this process instance came up
    beat_at_ms     INTEGER NOT NULL,     -- wall clock of the most recent beat
    queue_depth    INTEGER NOT NULL,     -- TelemetryDB.qsize() at the beat
    dropped_rows   INTEGER NOT NULL      -- TelemetryDB.dropped_count() at the beat
);

-- One row per periodic account poll (execution/account_monitor.py, hosted in the executor
-- process, which already holds a signed rest_client). History, not current-state-only, unlike
-- heartbeats -- unlike a liveness beat, a balance trend over time is itself something worth
-- keeping. balance_dollars is Kalshi's own fixed-point dollar string, read verbatim from
-- GET /portfolio/balance and never recomputed, per this schema's TEXT-fixed-point convention.
-- portfolio_value is stored as-is (observed as a bare integer in every live response so far;
-- its unit/scale is not documented and has always been 0 in this account's history, so it is
-- carried through untouched rather than assumed to mean anything). total_realized_pnl_dollars/
-- total_fees_paid_dollars/total_market_exposure_dollars are sums this process computes across
-- every row of GET /portfolio/positions' market_positions[] -- once summed, that is a derived
-- number, not a single wire value, so it is REAL like decision_results/index_observations'
-- engine-computed columns, not TEXT.
CREATE TABLE IF NOT EXISTS account_snapshots (
    id                            INTEGER PRIMARY KEY AUTOINCREMENT,
    balance_dollars               TEXT    NOT NULL,
    portfolio_value               REAL    NOT NULL,
    -- Gross realized PnL, summed from GET /portfolio/settlements (not /portfolio/positions,
    -- whose realized_pnl_dollars stays 0.0000 for every open position and vanishes with the row
    -- once a market settles, see account_monitor.py's module docstring).
    total_realized_pnl_dollars    REAL    NOT NULL,
    total_fees_paid_dollars       REAL    NOT NULL,
    -- total_realized_pnl_dollars minus total_fees_paid_dollars. Nullable, not NOT NULL: a bound
    -- NULL bypasses a column DEFAULT (`_insert()`'s `row.get(column)` sends one explicitly for
    -- any writer that predates this field), so NOT NULL here would have silently dropped their
    -- rows instead of defaulting them.
    total_net_realized_pnl_dollars REAL,
    total_market_exposure_dollars REAL    NOT NULL,
    open_position_count           INTEGER NOT NULL,
    snapshot_at_ms                INTEGER NOT NULL,
    created_at_ms                 INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER) * 1000)
);

CREATE INDEX IF NOT EXISTS idx_account_snapshots_snapshot_at ON account_snapshots (snapshot_at_ms);

-- Every fill this account received, from the WebSocket `fill` channel and, as a backstop, from
-- `GET /portfolio/fills`. Added by migration 6.
--
-- Why this table has to exist at all: `orders_fired.fill_count` comes from the response to
-- `POST /portfolio/events/orders`, which reports only the fills that happened *at placement*.
-- For a fill_or_kill order that is the complete final state; for a `good_till_canceled` order --
-- every maker quote -- it is `0.00`, and every later fill is invisible. A maker reconciled from
-- `orders_fired` alone reads as having never traded.
--
-- `fill_id` is the exchange's own identifier (`trade_id` on the WebSocket, `fill_id` on REST --
-- the spec states they are the same value) and is UNIQUE, which is what makes the two sources
-- idempotent against each other: the WebSocket writes a fill as it happens, the REST backstop
-- re-offers every fill it can see, and `INSERT OR IGNORE` keeps exactly one row either way.
--
-- `source` records which arrived first, and that is the gap detector. The `fill` channel carries
-- NO sequence number (`fillPayload` in the AsyncAPI spec requires only type/sid/msg, and msg has
-- no `seq`), unlike `orderbook_delta` -- so a dropped frame cannot be detected from the stream
-- itself. A row whose source is 'rest' is therefore a fill the live stream never delivered, and
-- counting those is the only honest gap measurement available.
CREATE TABLE IF NOT EXISTS fills (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    fill_id             TEXT    NOT NULL UNIQUE,   -- exchange's trade_id/fill_id
    order_id            TEXT    NOT NULL,          -- joins to orders_fired.kalshi_order_id
    client_order_id     TEXT,                      -- present on the WS payload, absent on REST
    ticker              TEXT    NOT NULL,
    outcome_side        TEXT    NOT NULL CHECK (outcome_side IN ('yes', 'no')),
    book_side           TEXT    CHECK (book_side IN ('bid', 'ask')),
    count_fp            TEXT    NOT NULL,          -- fixed-point contracts, genuinely fractional
    yes_price_dollars   TEXT    NOT NULL,
    -- The fee for THIS FILL IN TOTAL, not per contract. `CreateOrderV2Response.average_fee_paid`
    -- is a per-contract VWAP and the two must never be conflated: verified on a real 42-contract
    -- fill at 0.0020 carrying fee_cost 0.005900, which is 0.07*42*0.002*0.998 for the whole fill.
    fee_cost            TEXT,
    is_taker            INTEGER CHECK (is_taker IN (0, 1)),
    exchange_index      INTEGER,
    post_position_fp    TEXT,                      -- position after the fill; WS only
    source              TEXT    NOT NULL CHECK (source IN ('ws', 'rest')),
    filled_at_ms        INTEGER,                   -- exchange timestamp (ts_ms on WS)
    recorded_at_ms      INTEGER NOT NULL,
    created_at_ms       INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER) * 1000)
);

CREATE INDEX IF NOT EXISTS idx_fills_order_id ON fills (order_id);
CREATE INDEX IF NOT EXISTS idx_fills_ticker ON fills (ticker);
CREATE INDEX IF NOT EXISTS idx_fills_source ON fills (source);
