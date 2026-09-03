"""Tests for `telemetry.retention`.

Rows are inserted through raw SQL rather than `TelemetryDB.record_*` so each one's timestamp can
be placed precisely relative to a fixed `now`, which is what every retention boundary turns on.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from kalshi_bot.telemetry import retention
from kalshi_bot.telemetry.db import TelemetryDB

_NOW_MS = 1_787_000_000_000
_DAY_MS = 86_400_000


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = TelemetryDB(tmp_path / "telemetry.db")
    db.initialize()
    db.close()
    connection = retention.open_for_prune(tmp_path / "telemetry.db")
    yield connection
    connection.close()


def _correlation(conn: sqlite3.Connection, correlation_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO correlations (correlation_id, created_at_ms) VALUES (?, ?)",
        (correlation_id, _NOW_MS),
    )


def _latency(conn: sqlite3.Connection, correlation_id: str, age_days: float) -> None:
    _correlation(conn, correlation_id)
    conn.execute(
        "INSERT INTO latency_events (correlation_id, stage, started_at_ms, ended_at_ms, "
        "duration_ms) VALUES (?, 'wake_send', ?, ?, 1.0)",
        (correlation_id, int(_NOW_MS - age_days * _DAY_MS), _NOW_MS),
    )


def _decision(conn: sqlite3.Connection, correlation_id: str, age_days: float) -> None:
    _correlation(conn, correlation_id)
    conn.execute(
        "INSERT INTO decision_results (correlation_id, market_ticker, asset, should_fire, "
        "direction, model_probability, kalshi_price, fee, edge, ts_ms) "
        "VALUES (?, 'KXBTCD-T100', 'BTC', 0, 'yes', 0.5, 0.4, 0.01, 0.1, ?)",
        (correlation_id, int(_NOW_MS - age_days * _DAY_MS)),
    )


def _observation(conn: sqlite3.Connection, correlation_id: str, age_days: float) -> None:
    _correlation(conn, correlation_id)
    conn.execute(
        "INSERT INTO index_observations (correlation_id, asset, exchange, price, observed_at_ms) "
        "VALUES (?, 'BTC', 'coinbase', 100.0, ?)",
        (correlation_id, int(_NOW_MS - age_days * _DAY_MS)),
    )


def _order(conn: sqlite3.Connection, correlation_id: str, age_days: float) -> None:
    _correlation(conn, correlation_id)
    conn.execute(
        "INSERT INTO orders_fired (correlation_id, client_order_id, ticker, outcome_side, count, "
        "price_dollars, time_in_force, self_trade_prevention_type, status, requested_at_ms) "
        "VALUES (?, ?, 'KXBTCD-T100', 'yes', 1, '0.5000', 'fill_or_kill', 'taker_at_cross', "
        "'submitted', ?)",
        (correlation_id, f"cli-{correlation_id}", int(_NOW_MS - age_days * _DAY_MS)),
    )


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608


def test_deletes_rows_past_retention_and_keeps_the_rest(conn: sqlite3.Connection) -> None:
    _latency(conn, "l-old", 31)
    _latency(conn, "l-new", 29)
    _decision(conn, "d-old", 31)
    _decision(conn, "d-new", 29)
    _observation(conn, "o-old", 15)
    _observation(conn, "o-new", 13)
    conn.commit()

    result = retention.prune(conn, _NOW_MS, checkpoint=False)

    assert _count(conn, "latency_events") == 1
    assert _count(conn, "decision_results") == 1
    assert _count(conn, "index_observations") == 1
    assert result.deleted["latency_events"] == 1
    assert result.deleted["decision_results"] == 1
    assert result.deleted["index_observations"] == 1


def test_never_prunes_orders_fired(conn: sqlite3.Connection) -> None:
    # orders_fired is the audit trail and the basis of the risk gate's warm start. Pruning it
    # would silently reset restart-durable limits.
    _order(conn, "ord-ancient", 3650)
    conn.commit()

    retention.prune(conn, _NOW_MS, checkpoint=False)

    assert _count(conn, "orders_fired") == 1
    assert "orders_fired" not in retention._RETENTION


def test_retains_a_correlation_still_referenced_by_a_kept_order(conn: sqlite3.Connection) -> None:
    # The correlation is ancient and its latency rows are prunable, but an orders_fired row still
    # points at it. Deleting it would violate the foreign key and abort the prune.
    _order(conn, "shared", 3650)
    _latency(conn, "shared", 31)
    conn.commit()

    retention.prune(conn, _NOW_MS, checkpoint=False)

    assert _count(conn, "latency_events") == 0
    assert _count(conn, "orders_fired") == 1
    assert _count(conn, "correlations") == 1


def test_sweeps_correlations_nothing_references_any_more(conn: sqlite3.Connection) -> None:
    _latency(conn, "orphaned", 31)
    conn.commit()
    assert _count(conn, "correlations") == 1

    result = retention.prune(conn, _NOW_MS, checkpoint=False)

    assert _count(conn, "correlations") == 0
    assert result.deleted["correlations"] == 1


def test_is_a_no_op_on_an_empty_database(conn: sqlite3.Connection) -> None:
    result = retention.prune(conn, _NOW_MS, checkpoint=False)

    assert result.total == 0
    assert set(result.deleted) == {*retention._RETENTION, "correlations"}


def test_deletes_across_multiple_chunks(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The chunk loop must keep going until nothing is left, not stop after one pass.
    monkeypatch.setattr(retention, "_CHUNK_SIZE", 10)
    for i in range(35):
        _observation(conn, f"obs-{i}", 15)
    conn.commit()

    result = retention.prune(conn, _NOW_MS, checkpoint=False)

    assert result.deleted["index_observations"] == 35
    assert _count(conn, "index_observations") == 0


def test_boundary_row_exactly_at_the_cutoff_is_kept(conn: sqlite3.Connection) -> None:
    # Retention is "older than", not "at least as old as". An off-by-one here quietly deletes a
    # day more than documented.
    _observation(conn, "exactly-14d", 14)
    conn.commit()

    retention.prune(conn, _NOW_MS, checkpoint=False)

    assert _count(conn, "index_observations") == 1


def test_checkpoint_truncates_the_wal(tmp_path: Path) -> None:
    # Deleting rows grows the WAL rather than shrinking the database, so without the checkpoint a
    # prune makes disk usage temporarily worse.
    db = TelemetryDB(tmp_path / "telemetry.db")
    db.initialize()
    db.close()
    connection = retention.open_for_prune(tmp_path / "telemetry.db")
    try:
        for i in range(200):
            _observation(connection, f"obs-{i}", 8)
        connection.commit()
        wal = tmp_path / "telemetry.db-wal"
        assert wal.exists() and wal.stat().st_size > 0

        retention.prune(connection, _NOW_MS, checkpoint=True)

        assert wal.stat().st_size == 0
    finally:
        connection.close()
