"""Tests for `TelemetryDB`.

Exercises `initialize()` and all three `record_*` methods against a temp SQLite file. Since writes
are fire-and-forget (see `docs/GUIDE.md`), assertions read
back rows only after `close()`, which flushes the background writer thread's queue before
returning.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from kalshi_bot.telemetry.db import LATENCY_STAGES, TelemetryDB


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def test_initialize_creates_all_tables(tmp_path: Path) -> None:
    db = TelemetryDB(tmp_path / "telemetry.db")
    db.initialize()
    db.close()

    conn = _connect(db.db_path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    finally:
        conn.close()
    table_names = {row["name"] for row in rows}
    assert {
        "correlations",
        "orders_fired",
        "market_snapshots",
        "latency_events",
        "index_observations",
        "decision_results",
    } <= table_names


def test_initialize_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "telemetry.db"
    db1 = TelemetryDB(db_path)
    db1.initialize()
    db1.close()

    db2 = TelemetryDB(db_path)
    db2.initialize()
    db2.close()


def test_record_order_fired_writes_row_and_correlation(tmp_path: Path) -> None:
    db = TelemetryDB(tmp_path / "telemetry.db")
    db.initialize()
    db.record_order_fired(
        {
            "correlation_id": "corr-1",
            "client_order_id": "client-1",
            "kalshi_order_id": None,
            "ticker": "KXTEST",
            "outcome_side": "yes",
            "count": "10.00",
            "price_dollars": "0.5000",
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
            "status": "pending",
            "error_message": None,
            "requested_at_ms": 1000,
            "submitted_at_ms": None,
            "acknowledged_at_ms": None,
        }
    )
    db.close()

    conn = _connect(db.db_path)
    try:
        order_row = conn.execute("SELECT * FROM orders_fired").fetchone()
        correlation_row = conn.execute("SELECT * FROM correlations").fetchone()
    finally:
        conn.close()

    assert order_row["client_order_id"] == "client-1"
    assert order_row["correlation_id"] == "corr-1"
    assert correlation_row["correlation_id"] == "corr-1"


def test_record_market_snapshot_writes_row(tmp_path: Path) -> None:
    db = TelemetryDB(tmp_path / "telemetry.db")
    db.initialize()
    db.record_market_snapshot(
        {
            "correlation_id": "corr-2",
            "ticker": "KXTEST",
            "yes_bid_dollars": "0.4000",
            "yes_ask_dollars": "0.4200",
            "no_bid_dollars": "0.5800",
            "no_ask_dollars": "0.6000",
            "volume": "100.00",
            "open_interest": "500.00",
            "source": "poll",
            "observed_at_ms": 2000,
        }
    )
    db.close()

    conn = _connect(db.db_path)
    try:
        row = conn.execute("SELECT * FROM market_snapshots").fetchone()
    finally:
        conn.close()

    assert row["ticker"] == "KXTEST"
    assert row["source"] == "poll"


def test_record_latency_event_writes_row(tmp_path: Path) -> None:
    db = TelemetryDB(tmp_path / "telemetry.db")
    db.initialize()
    db.record_latency_event(
        {
            "correlation_id": "corr-3",
            "stage": "sign",
            "started_at_ms": 3000,
            "ended_at_ms": 3002,
            "duration_ms": 2.0,
            "metadata_json": None,
        }
    )
    db.close()

    conn = _connect(db.db_path)
    try:
        row = conn.execute("SELECT * FROM latency_events").fetchone()
    finally:
        conn.close()

    assert row["stage"] == "sign"
    assert row["duration_ms"] == 2.0


def test_record_latency_event_accepts_wake_send_and_wake_recv_stages(tmp_path: Path) -> None:
    db = TelemetryDB(tmp_path / "telemetry.db")
    db.initialize()
    db.record_latency_event(
        {
            "correlation_id": "corr-wake-1",
            "stage": "wake_send",
            "started_at_ms": 8000,
            "ended_at_ms": 8001,
            "duration_ms": 1.0,
            "metadata_json": None,
        }
    )
    db.record_latency_event(
        {
            "correlation_id": "corr-wake-1",
            "stage": "wake_recv",
            "started_at_ms": 8002,
            "ended_at_ms": 8003,
            "duration_ms": 1.0,
            "metadata_json": None,
        }
    )
    db.close()

    conn = _connect(db.db_path)
    try:
        stages = {
            row["stage"]
            for row in conn.execute(
                "SELECT stage FROM latency_events WHERE correlation_id = 'corr-wake-1'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert stages == {"wake_send", "wake_recv"}


def test_record_latency_event_rejects_invalid_stage(tmp_path: Path) -> None:
    """An unknown stage raises at the call site, not silently in the writer thread.

    The rejection used to be a SQLite `CHECK`, which fired on the writer thread where nothing
    could see it: the caller had already returned, and `_write_row` logged and dropped the row.
    A typo'd stage name produced a silent gap in the latency data instead of a failure. The
    check now lives in `record_latency_event` and this test asserts the raise, which is the whole
    behavioral difference.
    """
    db = TelemetryDB(tmp_path / "telemetry.db")
    db.initialize()
    try:
        with pytest.raises(ValueError, match="not-a-real-stage"):
            db.record_latency_event(
                {
                    "correlation_id": "corr-wake-2",
                    "stage": "not-a-real-stage",
                    "started_at_ms": 9000,
                    "ended_at_ms": 9001,
                    "duration_ms": 1.0,
                    "metadata_json": None,
                }
            )
    finally:
        db.close()

    conn = _connect(db.db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM latency_events WHERE correlation_id = 'corr-wake-2'"
        ).fetchone()["n"]
    finally:
        conn.close()
    assert count == 0


def test_record_latency_event_accepts_every_declared_stage(tmp_path: Path) -> None:
    """Every name in `LATENCY_STAGES` survives a real insert.

    Guards the seam the CHECK used to cover from the other side: with the constraint gone, a
    stage present in the frozenset but rejected by the table would be dropped on the writer
    thread, silently, exactly as before.
    """
    db = TelemetryDB(tmp_path / "telemetry.db")
    db.initialize()
    for index, stage in enumerate(sorted(LATENCY_STAGES)):
        db.record_latency_event(
            {
                "correlation_id": f"corr-stage-{index}",
                "stage": stage,
                "started_at_ms": 9000,
                "ended_at_ms": 9001,
                "duration_ms": 1.0,
                "metadata_json": None,
            }
        )
    db.close()

    conn = _connect(db.db_path)
    try:
        written = {row["stage"] for row in conn.execute("SELECT stage FROM latency_events")}
    finally:
        conn.close()
    assert written == set(LATENCY_STAGES)


def test_record_index_observation_writes_row(tmp_path: Path) -> None:
    db = TelemetryDB(tmp_path / "telemetry.db")
    db.initialize()
    db.record_index_observation(
        {
            "correlation_id": "corr-5",
            "asset": "BTC",
            "exchange": "coinbase",
            "price": 72665.07,
            "fair_value_index": 72670.12,
            "observed_at_ms": 5000,
        }
    )
    db.close()

    conn = _connect(db.db_path)
    try:
        row = conn.execute("SELECT * FROM index_observations").fetchone()
    finally:
        conn.close()

    assert row["asset"] == "BTC"
    assert row["exchange"] == "coinbase"
    assert row["price"] == 72665.07
    assert row["fair_value_index"] == 72670.12


def test_record_decision_result_writes_row(tmp_path: Path) -> None:
    db = TelemetryDB(tmp_path / "telemetry.db")
    db.initialize()
    db.record_decision_result(
        {
            "correlation_id": "corr-6",
            "market_ticker": "KXBTCD-T100",
            "asset": "BTC",
            "should_fire": 1,
            "direction": "yes",
            "model_probability": 0.62,
            "kalshi_price": 0.50,
            "fee": 0.02,
            "edge": 0.12,
            "ts_ms": 6000,
        }
    )
    db.close()

    conn = _connect(db.db_path)
    try:
        row = conn.execute("SELECT * FROM decision_results").fetchone()
    finally:
        conn.close()

    assert row["market_ticker"] == "KXBTCD-T100"
    assert row["asset"] == "BTC"
    assert row["should_fire"] == 1
    assert row["direction"] == "yes"
    assert row["model_probability"] == 0.62
    assert row["kalshi_price"] == 0.50
    assert row["fee"] == 0.02
    assert row["edge"] == 0.12
    assert row["ts_ms"] == 6000


def test_record_decision_result_rejects_invalid_direction(tmp_path: Path) -> None:
    db = TelemetryDB(tmp_path / "telemetry.db")
    db.initialize()
    db.record_decision_result(
        {
            "correlation_id": "corr-7",
            "market_ticker": "KXBTCD-T100",
            "asset": "BTC",
            "should_fire": 0,
            "direction": "not-a-real-direction",
            "model_probability": 0.5,
            "kalshi_price": 0.5,
            "fee": 0.02,
            "edge": 0.0,
            "ts_ms": 7000,
        }
    )
    db.close()  # must not raise, even though the write above violates the direction CHECK

    conn = _connect(db.db_path)
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM decision_results").fetchone()["n"]
    finally:
        conn.close()
    assert count == 0


def test_multiple_rows_share_one_correlation_row(tmp_path: Path) -> None:
    db = TelemetryDB(tmp_path / "telemetry.db")
    db.initialize()
    db.record_market_snapshot(
        {
            "correlation_id": "corr-shared",
            "ticker": "KXTEST",
            "yes_bid_dollars": None,
            "yes_ask_dollars": None,
            "no_bid_dollars": None,
            "no_ask_dollars": None,
            "volume": None,
            "open_interest": None,
            "source": "ws",
            "observed_at_ms": 4000,
        }
    )
    db.record_latency_event(
        {
            "correlation_id": "corr-shared",
            "stage": "ingest_fetch",
            "started_at_ms": 4000,
            "ended_at_ms": 4001,
            "duration_ms": 1.0,
            "metadata_json": None,
        }
    )
    db.close()

    conn = _connect(db.db_path)
    try:
        correlation_rows = conn.execute(
            "SELECT * FROM correlations WHERE correlation_id = 'corr-shared'"
        ).fetchall()
    finally:
        conn.close()

    assert len(correlation_rows) == 1


def test_malformed_row_is_dropped_not_raised(tmp_path: Path) -> None:
    db = TelemetryDB(tmp_path / "telemetry.db")
    db.initialize()
    # Missing required NOT NULL columns (ticker, outcome_side, count, ...) and violates the
    # time_in_force CHECK.
    db.record_order_fired({"correlation_id": "corr-bad", "time_in_force": "not-a-real-value"})
    db.close()  # must not raise, even though the write above fails inside the writer thread

    conn = _connect(db.db_path)
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM orders_fired").fetchone()["n"]
    finally:
        conn.close()
    assert count == 0


def test_record_before_initialize_raises(tmp_path: Path) -> None:
    db = TelemetryDB(tmp_path / "telemetry.db")
    with pytest.raises(RuntimeError):
        db.record_latency_event(
            {
                "correlation_id": "corr-4",
                "stage": "dispatch_send",
                "started_at_ms": 1,
                "ended_at_ms": 2,
                "duration_ms": 1.0,
                "metadata_json": None,
            }
        )


def test_close_is_idempotent(tmp_path: Path) -> None:
    db = TelemetryDB(tmp_path / "telemetry.db")
    db.initialize()
    db.close()
    db.close()


def test_busy_timeout_is_set_on_the_database(tmp_path: Path) -> None:
    # Without this, SQLite fails instantly when the other hot-path process holds the write lock,
    # and `_write_batch` swallows the SQLITE_BUSY as a silently dropped row.
    db = TelemetryDB(tmp_path / "telemetry.db")
    db.initialize()
    db.close()

    conn = sqlite3.connect(tmp_path / "telemetry.db")
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()


def test_queue_metrics_start_at_zero(tmp_path: Path) -> None:
    db = TelemetryDB(tmp_path / "telemetry.db")
    db.initialize()
    try:
        assert db.qsize() == 0
        assert db.dropped_count() == 0
    finally:
        db.close()


def test_a_batch_of_rows_all_land(tmp_path: Path) -> None:
    # Batching commits must not lose rows at a batch boundary; 1200 crosses the 500-row batch
    # size twice.
    db = TelemetryDB(tmp_path / "telemetry.db")
    db.initialize()
    for i in range(1200):
        db.record_decision_result(_decision_row(f"corr-{i}"))
    db.close()

    conn = _connect(tmp_path / "telemetry.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM decision_results").fetchone()[0] == 1200
    finally:
        conn.close()


def test_one_bad_row_does_not_take_down_the_rest_of_its_batch(tmp_path: Path) -> None:
    # A batch is one transaction, so a constraint violation would roll back every good row
    # alongside it. The writer retries the batch row-by-row so exactly the bad row is dropped.
    db = TelemetryDB(tmp_path / "telemetry.db")
    db.initialize()
    db.record_decision_result(_decision_row("corr-good-1"))
    db.record_decision_result({"market_ticker": None})  # violates NOT NULL
    db.record_decision_result(_decision_row("corr-good-2"))
    db.close()

    conn = _connect(tmp_path / "telemetry.db")
    try:
        tickers = [
            r["correlation_id"] for r in conn.execute("SELECT correlation_id FROM decision_results")
        ]
    finally:
        conn.close()

    assert sorted(tickers) == ["corr-good-1", "corr-good-2"]


def _stalled_db(tmp_path: Path) -> TelemetryDB:
    """A TelemetryDB whose queue accepts rows but whose writer never drains them.

    `initialize()` starts a real writer thread, which makes any full-queue assertion a race. Here
    the thread is never started and `_writer_thread` is set to a stand-in purely to satisfy
    `_enqueue`'s initialization guard, so the bounded-queue behavior is observable exactly.
    """
    db = TelemetryDB(tmp_path / "telemetry.db")
    db._writer_thread = threading.Thread(target=lambda: None)
    return db


def test_a_full_queue_drops_rows_and_counts_them(tmp_path: Path) -> None:
    from kalshi_bot.telemetry import db as db_module

    db = _stalled_db(tmp_path)
    observation = {"asset": "BTC", "exchange": "coinbase", "price": 1.0, "observed_at_ms": 1}
    for _ in range(db_module._MAX_QUEUE_SIZE):
        db.record_index_observation(dict(observation))

    assert db.qsize() == db_module._MAX_QUEUE_SIZE
    assert db.dropped_count() == 0

    for _ in range(50):
        db.record_index_observation(dict(observation))

    # Bounded, not grown; and every rejected row is accounted for rather than vanishing.
    assert db.qsize() == db_module._MAX_QUEUE_SIZE
    assert db.dropped_count() == 50


def test_drop_warnings_are_rate_limited(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    # A saturated queue drops thousands of rows a second. One log line per drop would cost more
    # than the writes it is failing to keep up with.
    from kalshi_bot.telemetry import db as db_module

    db = _stalled_db(tmp_path)
    observation = {"asset": "BTC", "exchange": "coinbase", "price": 1.0, "observed_at_ms": 1}
    for _ in range(db_module._MAX_QUEUE_SIZE):
        db.record_index_observation(dict(observation))

    with caplog.at_level("WARNING"):
        for _ in range(500):
            db.record_index_observation(dict(observation))

    assert db.dropped_count() == 500
    assert len([r for r in caplog.records if "telemetry queue full" in r.message]) == 1


def test_orders_fired_is_never_dropped_for_a_droppable_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # orders_fired is the audit trail of what this bot did with money and the basis for the risk
    # gate's warm start. It must not be lost to a backlog of price ticks.
    from kalshi_bot.telemetry import db as db_module

    monkeypatch.setattr(db_module, "_UNDROPPABLE_ENQUEUE_TIMEOUT_SECONDS", 0.05)
    db = _stalled_db(tmp_path)
    observation = {"asset": "BTC", "exchange": "coinbase", "price": 1.0, "observed_at_ms": 1}
    for _ in range(db_module._MAX_QUEUE_SIZE):
        db.record_index_observation(dict(observation))

    # A droppable row is dropped immediately...
    db.record_index_observation(dict(observation))
    assert db.dropped_count() == 1

    # ...while an order row waits for space instead of being discarded on the spot.
    started = time.monotonic()
    db.record_order_fired({"correlation_id": "c", "ticker": "KXBTCD-T100"})
    waited = time.monotonic() - started

    assert waited >= 0.05


def test_orders_fired_enqueues_normally_when_there_is_space(tmp_path: Path) -> None:
    db = TelemetryDB(tmp_path / "telemetry.db")
    db.initialize()
    db.record_order_fired(
        {
            "correlation_id": "corr-1",
            "client_order_id": "cli-1",
            "ticker": "KXBTCD-T100",
            "outcome_side": "yes",
            "count": 1,
            "price_dollars": "0.5000",
            "time_in_force": "fill_or_kill",
            "self_trade_prevention_type": "taker_at_cross",
            "status": "submitted",
            "requested_at_ms": 1,
        }
    )
    db.close()

    conn = _connect(tmp_path / "telemetry.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM orders_fired").fetchone()[0] == 1
    finally:
        conn.close()


def _decision_row(correlation_id: str) -> dict[str, object]:
    """A minimal valid `decision_results` row."""
    return {
        "correlation_id": correlation_id,
        "market_ticker": "KXBTCD-T100",
        "asset": "BTC",
        "should_fire": 0,
        "direction": "yes",
        "model_probability": 0.5,
        "kalshi_price": 0.4,
        "fee": 0.01,
        "edge": 0.1,
        "ts_ms": 1,
    }
