"""Ad-hoc schema migrations for an existing telemetry database, keyed on `PRAGMA user_version`.

`schema.sql` is `CREATE TABLE IF NOT EXISTS` throughout, which brings a *new* database to the
current shape and does nothing at all to an existing one. That is fine while changes are purely
additive, since a new table or a new index appears on the next open. It is not fine when an existing
table's definition has to change, which `CREATE TABLE IF NOT EXISTS` will silently decline to do,
leaving a database that looks initialized and is not.

This module is that gap and nothing more. It is deliberately not a migration framework: there is
no dependency graph, no down-migrations, and no autogeneration. It is an ordered list of functions
and an integer stored in the database file saying how many of them have run. `user_version` is a
four-byte field SQLite reserves for exactly this purpose and never touches itself.

Each migration runs inside a transaction and bumps `user_version` as part of it, so an interrupted
migration rolls back whole and re-runs on the next open rather than leaving a half-applied schema.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from typing import Final

logger = logging.getLogger(__name__)


def _drop_latency_stage_check(conn: sqlite3.Connection) -> None:
    """Rebuild `latency_events` without the `CHECK (stage IN (...))` constraint.

    The constraint was enforcing the stage vocabulary in the wrong place. `record_latency_event()`
    is called from the hot path and returns immediately; the row is written later, on the writer
    thread, where a `CHECK` violation raises a `sqlite3.Error` that `_write_row` logs and drops.
    There is no caller left to raise to by then. So a mistyped or newly-added stage name did not
    fail loudly at the call site. It produced a database with a silent hole in exactly the data
    the stage was added to collect, which is the failure mode latency instrumentation can least
    afford.

    Validation moves to a `frozenset` checked synchronously in `record_latency_event()`, which
    raises `ValueError` at the call site, in the caller's own thread, before the row is queued.
    That is strictly better on every axis: it fires at import-and-test time rather than in
    production telemetry, it names the offending stage, and it costs one set lookup.

    SQLite cannot drop a `CHECK` in place, so this is the standard twelve-step rebuild: create the
    replacement, copy, drop, rename. `id` values are preserved (they are copied explicitly, not
    regenerated) because `correlations` and any hand-written analysis query may reference them.
    """
    conn.executescript(
        """
        CREATE TABLE latency_events_new (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            correlation_id  TEXT    NOT NULL REFERENCES correlations (correlation_id),
            stage           TEXT    NOT NULL,
            started_at_ms   INTEGER NOT NULL,
            ended_at_ms     INTEGER NOT NULL,
            duration_ms     REAL    NOT NULL,
            metadata_json   TEXT,
            created_at_ms   INTEGER NOT NULL
                DEFAULT (CAST(strftime('%s', 'now') AS INTEGER) * 1000)
        );

        INSERT INTO latency_events_new
            (id, correlation_id, stage, started_at_ms, ended_at_ms, duration_ms,
             metadata_json, created_at_ms)
        SELECT id, correlation_id, stage, started_at_ms, ended_at_ms, duration_ms,
               metadata_json, created_at_ms
        FROM latency_events;

        DROP TABLE latency_events;
        ALTER TABLE latency_events_new RENAME TO latency_events;

        CREATE INDEX IF NOT EXISTS idx_latency_events_stage
            ON latency_events (stage);
        CREATE INDEX IF NOT EXISTS idx_latency_events_correlation_id
            ON latency_events (correlation_id);
        CREATE INDEX IF NOT EXISTS idx_latency_events_started_at
            ON latency_events (started_at_ms);
        """
    )


def _add_order_fill_columns(conn: sqlite3.Connection) -> None:
    """Add the four fill-reconciliation columns `CreateOrderV2Response` already returns.

    The dispatch response carries `fill_count`, `remaining_count`, `average_fill_price`, and
    `average_fee_paid`, which for a fill_or_kill order is the complete final state of the order,
    on the hot path, at no extra request. `order_dispatcher.py` used to read only `order_id` and
    throw the rest away, which is the sole reason position caps and PnL were not computable.

    Plain `ALTER TABLE ADD COLUMN`s, not a rebuild: the columns are nullable additions (error
    rows and pre-migration rows have no response to read), and the existing `status` CHECK
    already contains the wider vocabulary (`filled`/`canceled`/`accepted`/`rejected`) the
    dispatcher starts writing alongside them.
    """
    conn.executescript(
        """
        ALTER TABLE orders_fired ADD COLUMN fill_count         TEXT;
        ALTER TABLE orders_fired ADD COLUMN remaining_count    TEXT;
        ALTER TABLE orders_fired ADD COLUMN average_fill_price TEXT;
        ALTER TABLE orders_fired ADD COLUMN average_fee_paid   TEXT;
        """
    )


def _add_order_correlation_group_column(conn: sqlite3.Connection) -> None:
    """Add `orders_fired.correlation_group`, so the risk gate's per-group cap survives a restart.

    Two Kalshi series can be highly correlated (an hourly series and its 15-minute counterpart
    settle against the same underlying index) while trading under entirely distinct tickers, so
    the existing per-*ticker* caps never see them as related. `AssetConfig.correlation_group`
    (`decision/asset_registry.py`) already records which assets share exposure; this column is
    that value, carried onto the row that survives a restart, so `RiskGate.warm_start()` can
    rebuild the per-group counters without `execution`/`ipc` importing `decision` to re-derive
    them (see `ipc/protocol.py`'s "zero dependency on decision" note). Nullable: every
    pre-migration row, and every row written by a caller that predates this field, reads back
    NULL, which the risk gate treats the same as `''`: unknown, so skip the group cap for that
    row instead of guessing a group.
    """
    conn.execute("ALTER TABLE orders_fired ADD COLUMN correlation_group TEXT")


def _add_account_snapshot_net_pnl_column(conn: sqlite3.Connection) -> None:
    """Add `account_snapshots.total_net_realized_pnl_dollars`, gross PnL minus fees paid.

    `total_realized_pnl_dollars` alone isn't the figure an operator actually wants: it's gross,
    computed from `GET /portfolio/settlements`' `revenue`/`*_total_cost_dollars`, and does not
    reflect what fees already took out of the account. Nullable: every pre-migration row, and
    every row written by a caller that predates this field (`account_monitor.py`'s own tests
    among them), reads back NULL rather than failing the insert outright.

    Migrations run *before* `schema.sql`'s `CREATE TABLE IF NOT EXISTS` on every open (see
    `TelemetryDB.initialize()`), so a database old enough to need this migration but old enough
    to predate `account_snapshots` entirely (from before that table existed at all) would hit
    this `ALTER TABLE` before the table exists. Skip in that case, since `schema.sql` creates the
    table with the column already present a moment later, in the same `initialize()` call.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'account_snapshots'"
    ).fetchone()
    if exists is None:
        return
    conn.execute("ALTER TABLE account_snapshots ADD COLUMN total_net_realized_pnl_dollars REAL")


def _add_order_post_only_column(conn: sqlite3.Connection) -> None:
    """Add `orders_fired.post_only`, whether the order was sent as maker-only.

    `post_only` is a real `CreateOrderV2Request` field (verified against the OpenAPI spec on
    disk): a resting order that would take liquidity is cancelled rather than crossed. Whether a
    given order carried it decides whether that order could have paid the quadratic taker fee, and
    nothing in `CreateOrderV2Response` records the flag back, so the only place it can live is the
    row this bot writes.

    Nullable, with no `DEFAULT`: every pre-migration row was written by a dispatcher that did not
    send the field at all, and NULL is the honest record of that. `0` would assert the bot sent
    `post_only=false`, which is a different and false claim. The `CHECK (post_only IN (0, 1))` in
    `schema.sql` is not reproduced here. SQLite's `ALTER TABLE ADD COLUMN` accepts a CHECK, but a
    freshly created database gets the constraint from `schema.sql` and a migrated one is only ever
    written through `_TABLE_COLUMNS`, which sends an `int`.
    """
    conn.execute("ALTER TABLE orders_fired ADD COLUMN post_only INTEGER")


def _create_fills_table(conn: sqlite3.Connection) -> None:
    """Create `fills`, every fill this account received, from the WS channel and the REST backstop.

    A no-op on a fresh database, where `schema.sql`'s `CREATE TABLE IF NOT EXISTS` gets there
    first; it exists for the databases that already hold months of `orders_fired` history and
    would otherwise have no table to write a fill into.

    Kept byte-identical in shape to `schema.sql`'s definition, since the two are the same DDL, and
    the comment explaining every column lives there instead of being duplicated here.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fills (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            fill_id             TEXT    NOT NULL UNIQUE,
            order_id            TEXT    NOT NULL,
            client_order_id     TEXT,
            ticker              TEXT    NOT NULL,
            outcome_side        TEXT    NOT NULL CHECK (outcome_side IN ('yes', 'no')),
            book_side           TEXT    CHECK (book_side IN ('bid', 'ask')),
            count_fp            TEXT    NOT NULL,
            yes_price_dollars   TEXT    NOT NULL,
            fee_cost            TEXT,
            is_taker            INTEGER CHECK (is_taker IN (0, 1)),
            exchange_index      INTEGER,
            post_position_fp    TEXT,
            source              TEXT    NOT NULL CHECK (source IN ('ws', 'rest')),
            filled_at_ms        INTEGER,
            recorded_at_ms      INTEGER NOT NULL,
            created_at_ms       INTEGER NOT NULL
                DEFAULT (CAST(strftime('%s', 'now') AS INTEGER) * 1000)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fills_order_id ON fills (order_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fills_ticker ON fills (ticker)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fills_source ON fills (source)")


# Applied in order. A database's `user_version` is the count of entries already applied, so
# appending to this list is the only supported way to change it. Never reorder, never remove.
_MIGRATIONS: Final[tuple[Callable[[sqlite3.Connection], None], ...]] = (
    _drop_latency_stage_check,
    _add_order_fill_columns,
    _add_order_correlation_group_column,
    _add_account_snapshot_net_pnl_column,
    _add_order_post_only_column,
    _create_fills_table,
)

SCHEMA_VERSION: Final = len(_MIGRATIONS)


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Bring `conn`'s database up to `SCHEMA_VERSION`, running only what it has not seen.

    Safe to call on every open, including on a database `schema.sql` has just created: a fresh
    database is already in the current shape, so `initialize()` stamps it at `SCHEMA_VERSION`
    without running anything, and this returns immediately thereafter.

    Foreign keys are disabled for the duration. A rebuild-style migration drops and renames
    tables that other tables reference, and SQLite's documented procedure for that requires the
    constraint be off while the rename is in flight; leaving it on turns a correct migration into
    a constraint violation. It is restored before returning.

    Args:
        conn: An open write connection to the telemetry database.

    Returns:
        The number of migrations applied by this call. `0` means the database was current.

    Raises:
        sqlite3.Error: If a migration fails. The failing migration's transaction is rolled back,
            so `user_version` still reflects the last version that fully applied and the next
            open retries from there.
    """
    (current,) = conn.execute("PRAGMA user_version").fetchone()
    if current >= SCHEMA_VERSION:
        return 0

    pending = _MIGRATIONS[current:]
    logger.info(
        "telemetry schema at version %d, applying %d migration(s) to reach %d",
        current,
        len(pending),
        SCHEMA_VERSION,
    )
    # Must be outside the transaction: SQLite ignores a `foreign_keys` change made inside one.
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        for offset, migration in enumerate(pending):
            version = current + offset + 1
            conn.execute("BEGIN")
            try:
                migration(conn)
                # Not parameterizable, since PRAGMA does not accept bound parameters. `version` is a
                # loop index over a module-level tuple, never external input.
                conn.execute(f"PRAGMA user_version = {version}")
                conn.commit()
            except sqlite3.Error:
                conn.rollback()
                logger.exception("telemetry migration %d (%s) failed", version, migration.__name__)
                raise
            logger.info("telemetry migration %d (%s) applied", version, migration.__name__)
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return len(pending)


def stamp_current_version(conn: sqlite3.Connection) -> None:
    """Mark a database as already at `SCHEMA_VERSION` without running any migration.

    For a database `schema.sql` has just created from scratch: its tables are current by
    construction, and running a rebuild migration against them would be a no-op at best.
    """
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
