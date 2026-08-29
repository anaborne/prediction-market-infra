"""Tests for `telemetry.migrations`.

The thing worth testing about a migration is that an *existing* database
carrying real rows comes out the other side current and intact. So these build a database at the
old schema by hand, put rows in it, and then open it the way `TelemetryDB.initialize()` does.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from kalshi_bot.telemetry.db import TelemetryDB
from kalshi_bot.telemetry.migrations import SCHEMA_VERSION, apply_migrations

# The schema as it stood before any migration: `latency_events` with its `stage` CHECK
# (dropped by migration 1), `orders_fired` without the fill columns (added by migration 2), and
# the `correlations` parent both reference. Reproduced literally rather than imported, because
# the point is to build the schema this code no longer generates.
_OLD_SCHEMA = """
CREATE TABLE correlations (
    correlation_id  TEXT    PRIMARY KEY,
    created_at_ms   INTEGER NOT NULL
);

CREATE TABLE latency_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id  TEXT    NOT NULL REFERENCES correlations (correlation_id),
    stage           TEXT    NOT NULL CHECK (
        stage IN (
            'ingest_fetch', 'decision', 'order_build', 'sign', 'dispatch_send',
            'dispatch_ack', 'telemetry_write', 'wake_send', 'wake_recv'
        )
    ),
    started_at_ms   INTEGER NOT NULL,
    ended_at_ms     INTEGER NOT NULL,
    duration_ms     REAL    NOT NULL,
    metadata_json   TEXT,
    created_at_ms   INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER) * 1000)
);

CREATE TABLE orders_fired (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id              TEXT    NOT NULL REFERENCES correlations (correlation_id),
    client_order_id             TEXT    NOT NULL UNIQUE,
    kalshi_order_id             TEXT,
    ticker                      TEXT    NOT NULL,
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
    status                      TEXT    NOT NULL CHECK (
        status IN ('pending', 'submitted', 'accepted', 'rejected', 'filled', 'canceled', 'error')
    ),
    error_message               TEXT,
    requested_at_ms             INTEGER NOT NULL,
    submitted_at_ms             INTEGER,
    acknowledged_at_ms          INTEGER,
    created_at_ms               INTEGER NOT NULL
        DEFAULT (CAST(strftime('%s', 'now') AS INTEGER) * 1000)
);
"""


def _build_old_database(db_path: Path) -> None:
    """Create a pre-migration database with `latency_events` and `orders_fired` rows in it."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_OLD_SCHEMA)
        conn.execute("INSERT INTO correlations VALUES ('corr-old', 1000)")
        conn.executemany(
            "INSERT INTO latency_events "
            "(correlation_id, stage, started_at_ms, ended_at_ms, duration_ms, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("corr-old", "wake_send", 1000, 1001, 0.4, None),
                ("corr-old", "wake_recv", 1001, 1002, 0.3, '{"note": "kept"}'),
            ],
        )
        conn.execute(
            "INSERT INTO orders_fired "
            "(correlation_id, client_order_id, ticker, outcome_side, count, price_dollars, "
            " time_in_force, self_trade_prevention_type, status, requested_at_ms) "
            "VALUES ('corr-old', 'client-old', 'KXOLD', 'yes', '1.00', '0.4200', "
            "        'fill_or_kill', 'taker_at_cross', 'submitted', 1000)"
        )
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
    finally:
        conn.close()


def _rows(db_path: Path, sql: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _user_version(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        version: int = conn.execute("PRAGMA user_version").fetchone()[0]
        return version
    finally:
        conn.close()


def test_old_database_migrates_with_rows_preserved(tmp_path: Path) -> None:
    db_path = tmp_path / "telemetry.db"
    _build_old_database(db_path)

    db = TelemetryDB(db_path)
    db.initialize()
    db.close()

    rows = _rows(db_path, "SELECT * FROM latency_events ORDER BY id")
    assert [row["stage"] for row in rows] == ["wake_send", "wake_recv"]
    assert [row["id"] for row in rows] == [1, 2]
    assert rows[1]["metadata_json"] == '{"note": "kept"}'
    assert rows[0]["duration_ms"] == pytest.approx(0.4)
    assert _user_version(db_path) == SCHEMA_VERSION


def test_migrated_database_accepts_the_new_stage(tmp_path: Path) -> None:
    """The point of the migration: `detect_fire` was not in the old CHECK's vocabulary.

    Before the rebuild this insert would raise a `CHECK` violation on the writer thread, be
    logged, and be dropped, leaving no `detect_fire` data and no error anyone would see.
    """
    db_path = tmp_path / "telemetry.db"
    _build_old_database(db_path)

    db = TelemetryDB(db_path)
    db.initialize()
    db.record_latency_event(
        {
            "correlation_id": "corr-new",
            "stage": "detect_fire",
            "started_at_ms": 2000,
            "ended_at_ms": 2012,
            "duration_ms": 11.7,
            "metadata_json": '{"dry_run": true}',
        }
    )
    db.close()

    rows = _rows(db_path, "SELECT * FROM latency_events WHERE stage = 'detect_fire'")
    assert len(rows) == 1
    assert rows[0]["duration_ms"] == pytest.approx(11.7)


def test_old_orders_fired_gains_null_fill_columns_and_accepts_new_fill_rows(
    tmp_path: Path,
) -> None:
    """Migration 2: existing order rows survive with NULL fill columns; new rows can carry them.

    NULL, not `"0.00"`, on the old rows, because a pre-migration dispatch genuinely never read its
    response, and back-filling zeros would fabricate a reconciliation that never happened.
    """
    db_path = tmp_path / "telemetry.db"
    _build_old_database(db_path)

    db = TelemetryDB(db_path)
    db.initialize()
    db.record_order_fired(
        {
            "correlation_id": "corr-new",
            "client_order_id": "client-new",
            "ticker": "KXNEW",
            "outcome_side": "no",
            "count": "2.00",
            "price_dollars": "0.7000",
            "time_in_force": "fill_or_kill",
            "self_trade_prevention_type": "taker_at_cross",
            "status": "filled",
            "error_message": None,
            "fill_count": "2.00",
            "remaining_count": "0.00",
            "average_fill_price": "0.7000",
            "average_fee_paid": "0.0300",
            "requested_at_ms": 2000,
            "submitted_at_ms": 2001,
            "acknowledged_at_ms": 2002,
        }
    )
    db.close()

    old_row = _rows(db_path, "SELECT * FROM orders_fired WHERE client_order_id = 'client-old'")[0]
    assert old_row["status"] == "submitted"
    assert old_row["fill_count"] is None
    assert old_row["average_fill_price"] is None

    new_row = _rows(db_path, "SELECT * FROM orders_fired WHERE client_order_id = 'client-new'")[0]
    assert new_row["status"] == "filled"
    assert new_row["fill_count"] == "2.00"
    assert new_row["remaining_count"] == "0.00"
    assert new_row["average_fill_price"] == "0.7000"
    assert new_row["average_fee_paid"] == "0.0300"


def test_old_orders_fired_gains_null_correlation_group_and_accepts_new_group_rows(
    tmp_path: Path,
) -> None:
    """Migration 3: existing order rows survive with NULL `correlation_group`; new rows can
    carry a real one, which is what lets the risk gate's per-group cap warm-start correctly."""
    db_path = tmp_path / "telemetry.db"
    _build_old_database(db_path)

    db = TelemetryDB(db_path)
    db.initialize()
    db.record_order_fired(
        {
            "correlation_id": "corr-group",
            "client_order_id": "client-group",
            "ticker": "KXBTC15M-T100",
            "correlation_group": "majors",
            "outcome_side": "yes",
            "count": "1.00",
            "price_dollars": "0.5000",
            "time_in_force": "fill_or_kill",
            "self_trade_prevention_type": "taker_at_cross",
            "status": "filled",
            "fill_count": "1.00",
            "requested_at_ms": 3000,
        }
    )
    db.close()

    old_row = _rows(db_path, "SELECT * FROM orders_fired WHERE client_order_id = 'client-old'")[0]
    assert old_row["correlation_group"] is None

    new_row = _rows(db_path, "SELECT * FROM orders_fired WHERE client_order_id = 'client-group'")[0]
    assert new_row["correlation_group"] == "majors"


def test_a_database_predating_account_snapshots_entirely_still_migrates(tmp_path: Path) -> None:
    """Migration 4 runs before `schema.sql`'s `CREATE TABLE IF NOT EXISTS` on every open, so a
    database old enough to need it but old enough to predate `account_snapshots` entirely (the
    table itself as well as the column) must not crash trying to `ALTER TABLE` a table that
    doesn't exist yet. `schema.sql` creates it, with the column already present, moments later
    in the same `initialize()` call.
    """
    db_path = tmp_path / "telemetry.db"
    _build_old_database(db_path)  # _OLD_SCHEMA has no account_snapshots table at all

    db = TelemetryDB(db_path)
    db.initialize()
    db.record_account_snapshot(
        {
            "balance_dollars": "500.0000",
            "portfolio_value": 0.0,
            "total_realized_pnl_dollars": 0.30,
            "total_net_realized_pnl_dollars": 0.279,
            "total_fees_paid_dollars": 0.021,
            "total_market_exposure_dollars": 0.0,
            "open_position_count": 0,
            "snapshot_at_ms": 4000,
        }
    )
    db.close()

    assert _user_version(db_path) == SCHEMA_VERSION
    row = _rows(db_path, "SELECT * FROM account_snapshots")[0]
    assert row["total_net_realized_pnl_dollars"] == pytest.approx(0.279)


def test_old_account_snapshots_gain_a_null_net_pnl_column(tmp_path: Path) -> None:
    """A database that already has `account_snapshots` (from before this column existed) gets
    the column added in place; its existing rows read back NULL rather than failing the insert
    migration 4 performs."""
    db_path = tmp_path / "telemetry.db"
    _build_old_database(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE account_snapshots (
                id                            INTEGER PRIMARY KEY AUTOINCREMENT,
                balance_dollars               TEXT    NOT NULL,
                portfolio_value               REAL    NOT NULL,
                total_realized_pnl_dollars    REAL    NOT NULL,
                total_fees_paid_dollars       REAL    NOT NULL,
                total_market_exposure_dollars REAL    NOT NULL,
                open_position_count           INTEGER NOT NULL,
                snapshot_at_ms                INTEGER NOT NULL,
                created_at_ms                 INTEGER NOT NULL
                    DEFAULT (CAST(strftime('%s', 'now') AS INTEGER) * 1000)
            );
            INSERT INTO account_snapshots
                (balance_dollars, portfolio_value, total_realized_pnl_dollars,
                 total_fees_paid_dollars, total_market_exposure_dollars,
                 open_position_count, snapshot_at_ms)
            VALUES ('500.0000', 0.0, 0.0, 0.01, 1.0, 1, 1000);
            """
        )
        conn.execute("PRAGMA user_version = 3")  # already past migration 3, not yet 4
        conn.commit()
    finally:
        conn.close()

    db = TelemetryDB(db_path)
    db.initialize()
    db.close()

    old_row = _rows(db_path, "SELECT * FROM account_snapshots WHERE snapshot_at_ms = 1000")[0]
    assert old_row["total_net_realized_pnl_dollars"] is None
    assert _user_version(db_path) == SCHEMA_VERSION


def test_foreign_keys_are_re_enabled_after_migrating(tmp_path: Path) -> None:
    """The rebuild turns `foreign_keys` off; leaving it off would silently disarm the schema."""
    db_path = tmp_path / "telemetry.db"
    _build_old_database(db_path)

    conn = sqlite3.connect(db_path)
    try:
        apply_migrations(conn)
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_applying_migrations_twice_is_a_no_op(tmp_path: Path) -> None:
    db_path = tmp_path / "telemetry.db"
    _build_old_database(db_path)

    conn = sqlite3.connect(db_path)
    try:
        assert apply_migrations(conn) == SCHEMA_VERSION
        assert apply_migrations(conn) == 0
    finally:
        conn.close()


def test_a_fresh_database_is_stamped_current_without_migrating(tmp_path: Path) -> None:
    """A database `schema.sql` just created is already current; running a rebuild on it is waste."""
    db_path = tmp_path / "telemetry.db"

    db = TelemetryDB(db_path)
    db.initialize()
    db.close()

    assert _user_version(db_path) == SCHEMA_VERSION

    conn = sqlite3.connect(db_path)
    try:
        assert apply_migrations(conn) == 0
    finally:
        conn.close()


def test_reopening_a_current_database_preserves_its_rows(tmp_path: Path) -> None:
    """`initialize()` runs on every process start, including the second one."""
    db_path = tmp_path / "telemetry.db"

    first = TelemetryDB(db_path)
    first.initialize()
    first.record_latency_event(
        {
            "correlation_id": "corr-1",
            "stage": "detect_fire",
            "started_at_ms": 1,
            "ended_at_ms": 2,
            "duration_ms": 1.0,
            "metadata_json": None,
        }
    )
    first.close()

    second = TelemetryDB(db_path)
    second.initialize()
    second.close()

    assert len(_rows(db_path, "SELECT * FROM latency_events")) == 1
