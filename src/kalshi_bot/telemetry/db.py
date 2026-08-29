"""Telemetry database access.

Owns opening the SQLite telemetry database (applying `schema.sql` on first use) and recording
orders fired, market snapshots, and latency events.

Writes are fire-and-forget per `ENGINEERING.md`'s non-negotiable rule 4: `record_*` methods push the
row
onto an in-memory queue and return immediately. No SQLite I/O happens on the caller's thread, and
callers never `await` anything. A single background writer thread owns the actual `sqlite3`
connection and drains the queue. See `docs/GUIDE.md` for why
this shape (thread + queue) was chosen over an `asyncio.create_task` alternative.

A write that fails (e.g. a `CHECK`/`NOT NULL` violation from a malformed row) is logged and
dropped instead of raised, because there is no caller left to raise to by the time the writer thread
sees it, and telemetry must never be able to affect the hot path it is observing.

Two properties of the queue matter for long unattended runs, and the fire-and-forget telemetry rule
left both open:

- It is bounded. An unbounded queue turns a writer that cannot keep up into unbounded memory
  growth, which on a multi-day run ends the process rather than degrading it. Past the soft cap,
  rows are dropped at enqueue time and counted. `orders_fired` is exempt: it is the audit trail of
  what this bot did with money, and losing one of those to a backlog of price ticks is not a
  trade-off worth making.
- Commits are batched. One `commit()` per row means one fsync per row. Under WAL with
  `synchronous = NORMAL` that is survivable, but it caps throughput far below what the feeds can
  produce and makes the writer the bottleneck it was designed not to be. The writer now drains
  whatever is queued, up to a bounded batch, and commits once.

`qsize()` and `dropped_count()` expose the backlog for the heartbeat and the dashboard: a queue
that is growing and a drop count that is climbing are the two signals that distinguish "the writer
is behind" from "the feeds went quiet".
"""

from __future__ import annotations

import logging
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Final

from kalshi_bot.telemetry.migrations import apply_migrations, stamp_current_version

logger = logging.getLogger(__name__)

_SCHEMA_PATH: Final = Path(__file__).parent / "schema.sql"

# Every stage name `record_latency_event()` will accept. Enforced here, in the calling thread,
# instead of as a SQLite CHECK on `latency_events.stage`. See that column's comment in
# `schema.sql` and `migrations.py::_drop_latency_stage_check` for why the constraint moved.
#
# 'detect_fire' spans the whole pipeline and is measured end to end rather than summed from the
# others; the remaining stages decompose it, but not exhaustively, since the gaps between them
# belong to no stage. See `docs/GUIDE.md`.
LATENCY_STAGES: Final[frozenset[str]] = frozenset(
    {
        "detect_fire",
        "ingest_fetch",
        "decision",
        "wake_send",
        "wake_recv",
        "order_build",
        "sign",
        "dispatch_send",
        "dispatch_ack",
        "telemetry_write",
    }
)

# Soft cap on queued rows. Sized to absorb a writer stall at the feeds' sustained rate without
# being large enough for the backlog itself to matter (a queued row is a small dict; 10k of them
# is single-digit megabytes).
#
# H2's latency instrumentation roughly tripled that sustained rate, measured at ~70 rows/s across
# both processes, up from ~20/s, once every stage gained a call site. 10k is still ~2.5 minutes of
# headroom against a stalled writer, which is ample, but the stall it absorbs is now a third of
# what it was when this number was chosen. If the soak shows queue depth trending upward,
# `RunnerConfig.shadow_fire_every` is the dial to turn first: it is the only new row source whose
# rate is directly configurable.
_MAX_QUEUE_SIZE: Final = 10_000

# Rows drained per transaction. Bounds how much work one commit represents, so a crash loses at
# most this many rows and shutdown never waits on an unbounded drain.
_MAX_BATCH_SIZE: Final = 500

# A pseudo-table naming the "upsert this order's final state" job. It travels the same queue as
# every insert so ordering is preserved (a resolution can never overtake the pending row it
# repairs), but `_insert` routes it to an upsert keyed on `client_order_id` instead.
#
# Why an upsert rather than an update: the pending row that precedes it is enqueued
# non-blocking (see `record_order_fired`'s `blocking` argument) and can therefore be dropped
# when the queue is saturated. An UPDATE would then match nothing and the order would vanish from
# the audit trail, which is the exact hole this whole mechanism exists to close. The upsert
# repairs a missing pending row instead of silently doing nothing.
ORDER_RESOLUTION_JOB: Final = "orders_fired__resolution"

# Tables whose rows are never dropped, however deep the backlog. `orders_fired` is the record of
# real money movement and the basis for the risk gate's warm start.
_UNDROPPABLE_TABLES: Final[frozenset[str]] = frozenset(
    {"orders_fired", ORDER_RESOLUTION_JOB, "fills"}
)

# One warning per this interval, not one per dropped row: a saturated queue drops thousands of
# rows a second, and logging each one would put far more load on the process than the writes it
# is failing to keep up with.
_DROP_WARNING_INTERVAL_SECONDS: Final = 10.0

# How long a write waits for a competing writer's lock before giving up. Both hot-path processes
# hold write connections to this file; SQLite's default is to fail instantly on contention.
_BUSY_TIMEOUT_MS: Final = 5_000

# How long an undroppable row waits for queue space before it is finally given up on. Long enough
# to ride out a writer draining a full backlog, short enough that a wedged writer cannot hold an
# order path indefinitely.
_UNDROPPABLE_ENQUEUE_TIMEOUT_SECONDS: Final = 2.0

# Column order for each table, excluding autoincrement `id` and defaulted `created_at_ms`.
_TABLE_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    "orders_fired": (
        "correlation_id",
        "client_order_id",
        "kalshi_order_id",
        "ticker",
        "correlation_group",
        "outcome_side",
        "count",
        "price_dollars",
        "time_in_force",
        "self_trade_prevention_type",
        "post_only",
        "status",
        "error_message",
        "fill_count",
        "remaining_count",
        "average_fill_price",
        "average_fee_paid",
        "requested_at_ms",
        "submitted_at_ms",
        "acknowledged_at_ms",
    ),
    "fills": (
        "fill_id",
        "order_id",
        "client_order_id",
        "ticker",
        "outcome_side",
        "book_side",
        "count_fp",
        "yes_price_dollars",
        "fee_cost",
        "is_taker",
        "exchange_index",
        "post_position_fp",
        "source",
        "filled_at_ms",
        "recorded_at_ms",
    ),
    "market_snapshots": (
        "correlation_id",
        "ticker",
        "yes_bid_dollars",
        "yes_ask_dollars",
        "no_bid_dollars",
        "no_ask_dollars",
        "volume",
        "open_interest",
        "source",
        "observed_at_ms",
    ),
    "latency_events": (
        "correlation_id",
        "stage",
        "started_at_ms",
        "ended_at_ms",
        "duration_ms",
        "metadata_json",
    ),
    "index_observations": (
        "correlation_id",
        "asset",
        "exchange",
        "price",
        "fair_value_index",
        "observed_at_ms",
    ),
    "decision_results": (
        "correlation_id",
        "market_ticker",
        "asset",
        "should_fire",
        "direction",
        "model_probability",
        "kalshi_price",
        "fee",
        "edge",
        "ts_ms",
    ),
    "heartbeats": (
        "process",
        "pid",
        "started_at_ms",
        "beat_at_ms",
        "queue_depth",
        "dropped_rows",
    ),
    "account_snapshots": (
        "balance_dollars",
        "portfolio_value",
        "total_realized_pnl_dollars",
        "total_net_realized_pnl_dollars",
        "total_fees_paid_dollars",
        "total_market_exposure_dollars",
        "open_position_count",
        "snapshot_at_ms",
    ),
}

# Tables holding current state rather than history: writes are INSERT OR REPLACE keyed on the
# primary key, so each process's row is overwritten in place instead of accumulating.
_REPLACE_TABLES: Final = frozenset({"heartbeats"})

# Tables whose rows carry the exchange's own identifier and may legitimately be offered twice.
# `fills` is written from two sources on purpose (the WebSocket as it happens, and the REST
# backstop that catches what the stream missed) so re-offering a fill must be a no-op, not a
# UNIQUE violation that logs an error and drops the batch around it.
_IGNORE_TABLES: Final = frozenset({"fills"})

_WriteJob = tuple[str, dict[str, Any]]


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _has_tables(conn: sqlite3.Connection) -> bool:
    """Whether this database has already been initialized by a previous `initialize()` call.

    Distinguishes "existing database, may predate a schema change" from "empty file SQLite just
    created for us", which decides whether migrations run or the version is simply stamped.
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'latency_events'"
    ).fetchone()
    return row is not None


class TelemetryDB:
    """Connection wrapper around the telemetry SQLite database.

    Attributes:
        db_path: Filesystem path to the SQLite database file.
    """

    def __init__(self, db_path: Path) -> None:
        """Store the database path for later use by record/init methods.

        Args:
            db_path: Filesystem path to the SQLite database file.
        """
        self.db_path = db_path
        self._queue: queue.Queue[_WriteJob | None] = queue.Queue(maxsize=_MAX_QUEUE_SIZE)
        self._writer_thread: threading.Thread | None = None
        self._dropped = 0
        self._dropped_lock = threading.Lock()
        self._last_drop_warning = 0.0
        # Stamped at construction so every heartbeat reports when this process instance came up.
        self._started_at_ms = _now_ms()

    def initialize(self) -> None:
        """Create telemetry tables if they do not already exist, using `schema.sql`.

        Also starts the background writer thread that `record_*` methods hand rows to. Must be
        called (once) before any `record_*` call.
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        schema_sql = _SCHEMA_PATH.read_text()
        conn = sqlite3.connect(self.db_path)
        try:
            # WAL mode lets a separate read-only connection (dashboard.TelemetryReader) query
            # concurrently without hitting SQLITE_BUSY against this writer. Persisted in the
            # database file itself, so this only needs to run once, but is idempotent to repeat.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")

            # Migrations run before `schema.sql`, and only against a database that already has
            # tables. `schema.sql` is `CREATE TABLE IF NOT EXISTS` throughout, so it cannot alter
            # an existing table and will not undo a rebuild; running it afterwards just fills in
            # anything genuinely new. A database created fresh by this call is current by
            # construction and is stamped rather than migrated.
            if _has_tables(conn):
                apply_migrations(conn)
                conn.executescript(schema_sql)
            else:
                conn.executescript(schema_sql)
                stamp_current_version(conn)
            conn.commit()
        finally:
            conn.close()

        self._writer_thread = threading.Thread(
            target=self._run_writer, name="telemetry-writer", daemon=True
        )
        self._writer_thread.start()

    def record_order_fired(self, order: dict[str, Any], *, blocking: bool = True) -> None:
        """Enqueue an `orders_fired` insert. Returns immediately; never touches SQLite.

        Args:
            order: Order fields matching the `orders_fired` table columns.
            blocking: Whether a saturated queue may briefly apply backpressure. `orders_fired` is
                undroppable, so the default waits up to `_UNDROPPABLE_ENQUEUE_TIMEOUT_SECONDS` for
                space rather than losing the audit trail. Pass `False` from anywhere that sits
                before an order reaches the wire. `ENGINEERING.md` rule 4 forbids a telemetry write
                from delaying a dispatch, and a two-second stall in front of the socket write would
                be exactly that. The pre-dispatch `pending` row (`execution.order_dispatcher`) is
                the one caller that needs it; the row it may lose is repaired by the resolution
                upsert that follows.
        """
        self._enqueue("orders_fired", order, blocking=blocking)

    def record_order_resolution(self, order: dict[str, Any]) -> None:
        """Enqueue the final state of an order, upserted on `client_order_id`.

        Pairs with the `pending` row `record_order_fired(..., blocking=False)` writes before the
        request leaves. Written as an upsert, not an update, so that a pending row lost to a
        saturated queue is recreated rather than leaving the order absent from the audit trail
        entirely, the failure this whole mechanism exists to make impossible.

        Undroppable and therefore blocking, which is safe: by the time a resolution exists the
        order has already been sent, so this is no longer the stretch rule 4 protects.

        Args:
            order: The complete `orders_fired` row, `client_order_id` included.
        """
        self._enqueue(ORDER_RESOLUTION_JOB, order)

    def record_fill(self, fill: dict[str, Any]) -> None:
        """Enqueue a row for `fills`. Returns immediately; never touches SQLite.

        Undroppable, like `orders_fired`: a fill is a contract this account owns, and the whole
        reason the table exists is that losing one made a real position invisible. De-duplicated
        on `fill_id` by `INSERT OR IGNORE`, so the WebSocket consumer and the REST backstop can
        both offer the same fill without either needing to know about the other.

        Args:
            fill: Fill fields matching the `fills` table columns.
        """
        self._enqueue("fills", fill)

    def record_market_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Enqueue a row for `market_snapshots`. Returns immediately; never touches SQLite.

        Args:
            snapshot: Snapshot fields matching the `market_snapshots` table columns.
        """
        self._enqueue("market_snapshots", snapshot)

    def record_latency_event(self, event: dict[str, Any]) -> None:
        """Enqueue a row for `latency_events`. Returns immediately; never touches SQLite.

        `event["stage"]` is validated against `LATENCY_STAGES` here, synchronously, before the
        row is queued. This is the one `record_*` method that can raise, and deliberately so: an
        unrecognized stage is a bug in the calling code, and the alternative of letting the writer
        thread discover it means the bug shows up as missing rows in the exact dataset that was
        added to be measured. One `frozenset` lookup on the hot path buys a failure at the call
        site, in a test, instead.

        Args:
            event: Event fields matching the `latency_events` table columns.

        Raises:
            ValueError: If `event["stage"]` is not in `LATENCY_STAGES`.
        """
        stage = event.get("stage")
        if stage not in LATENCY_STAGES:
            raise ValueError(
                f"unknown latency stage {stage!r}; expected one of {sorted(LATENCY_STAGES)}"
            )
        self._enqueue("latency_events", event)

    def record_index_observation(self, observation: dict[str, Any]) -> None:
        """Enqueue a row for `index_observations`. Returns immediately; never touches SQLite.

        Callers (e.g. `ingest.exchange_feed`) are responsible for sampling/throttling before
        calling this, since every tick from a free exchange feed need not be written, and the write
        volume this module was designed around is far below a raw feed's tick rate.

        Args:
            observation: Fields matching the `index_observations` table columns.
        """
        self._enqueue("index_observations", observation)

    def record_decision_result(self, result: dict[str, Any]) -> None:
        """Enqueue a row for `decision_results`. Returns immediately; never touches SQLite.

        Callers (e.g. `decision.runner`) are responsible for sampling non-firing results before
        calling this. `should_fire=True` rows should always be recorded, but every strike
        evaluation need not be; see `docs/GUIDE.md`.

        Args:
            result: Fields matching the `decision_results` table columns.
        """
        self._enqueue("decision_results", result)

    def record_heartbeat(self, process: str) -> None:
        """Enqueue this process's liveness beat. Returns immediately; never touches SQLite.

        Upserted (the `heartbeats` table holds current state, one row per process), carrying
        this instance's own `qsize()`/`dropped_count()`, the only way another process (the
        dashboard's `/health`) can see these in-memory counters.

        Args:
            process: Stable name of the beating process, e.g. `"poller"` or `"executor"`.
        """
        self._enqueue(
            "heartbeats",
            {
                "process": process,
                "pid": os.getpid(),
                "started_at_ms": self._started_at_ms,
                "beat_at_ms": _now_ms(),
                "queue_depth": self.qsize(),
                "dropped_rows": self.dropped_count(),
            },
        )

    def record_account_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Enqueue a row for `account_snapshots`. Returns immediately; never touches SQLite.

        Args:
            snapshot: Fields matching the `account_snapshots` table columns, built by
                `execution.account_monitor.fetch_account_snapshot()`.
        """
        self._enqueue("account_snapshots", snapshot)

    def close(self) -> None:
        """Flush any queued writes and stop the background writer thread.

        Idempotent. A second call after the thread has already stopped is a no-op.
        """
        if self._writer_thread is None:
            return
        # Blocking, not `put_nowait`: the queue is bounded now, and a full queue at shutdown is
        # exactly the case where flushing matters. The writer is draining, so space arrives.
        self._queue.put(None)
        self._writer_thread.join()
        self._writer_thread = None

    def _enqueue(self, table: str, row: dict[str, Any], *, blocking: bool = True) -> None:
        if self._writer_thread is None:
            raise RuntimeError("TelemetryDB.initialize() must be called before recording events")
        try:
            self._queue.put_nowait((table, row))
        except queue.Full:
            if table in _UNDROPPABLE_TABLES and not blocking:
                # A caller that cannot afford backpressure, sitting in front of an order reaching
                # the wire (`ENGINEERING.md` rule 4). Losing the row is the lesser harm, and it is
                # named rather than silent: the ERROR below carries the client_order_id, the drop
                # counter climbs, and the resolution upsert that follows recreates the row.
                logger.error(
                    "telemetry queue full; DROPPED A NON-BLOCKING %s ROW (client_order_id=%r); "
                    "the resolution upsert will recreate it",
                    table.upper(),
                    row.get("client_order_id"),
                )
                self._record_drop(table)
                return
            if table in _UNDROPPABLE_TABLES:
                # Block rather than lose the audit trail. This is the one place telemetry can
                # apply backpressure to a caller, and it is bounded by the writer draining a
                # batch, but it means a wedged writer can stall an order path, so the wait is
                # capped and a failure to enqueue is logged rather than raised.
                try:
                    self._queue.put((table, row), timeout=_UNDROPPABLE_ENQUEUE_TIMEOUT_SECONDS)
                    return
                except queue.Full:
                    logger.error(
                        "telemetry queue still full after %.1fs; DROPPED AN %s ROW: %r",
                        _UNDROPPABLE_ENQUEUE_TIMEOUT_SECONDS,
                        table.upper(),
                        row,
                    )
            self._record_drop(table)

    def _record_drop(self, table: str) -> None:
        """Count a dropped row and warn at most once per interval."""
        with self._dropped_lock:
            self._dropped += 1
            total = self._dropped
            now = time.monotonic()
            should_warn = now - self._last_drop_warning >= _DROP_WARNING_INTERVAL_SECONDS
            if should_warn:
                self._last_drop_warning = now
        if should_warn:
            logger.warning(
                "telemetry queue full (%d rows); dropping writes, most recently to %s. "
                "%d rows dropped in total since start.",
                _MAX_QUEUE_SIZE,
                table,
                total,
            )

    def qsize(self) -> int:
        """Approximate number of rows waiting to be written.

        Exposed for the process heartbeat and the dashboard. A queue depth that trends upward is
        the signal that the writer is not keeping up, well before rows start being dropped.
        """
        return self._queue.qsize()

    def dropped_count(self) -> int:
        """Total rows dropped because the queue was full, since this instance started."""
        with self._dropped_lock:
            return self._dropped

    def _run_writer(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA synchronous = NORMAL")
            # Without this, SQLite's default busy handler gives up immediately on a locked
            # database, `_write_batch`'s `except sqlite3.Error` swallows the SQLITE_BUSY, and the
            # rows are silently dropped. Both hot-path processes open write connections to this
            # same file, so that contention is routine rather than exceptional.
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            while True:
                batch = self._drain_batch()
                if batch is None:
                    break
                self._write_batch(conn, batch)
        finally:
            conn.close()

    def _drain_batch(self) -> list[_WriteJob] | None:
        """Block for one job, then take whatever else is already queued, up to the batch size.

        Returns:
            The drained jobs, or `None` once the shutdown sentinel is seen. A sentinel encountered
            partway through a batch still returns the rows drained before it, so `close()` never
            discards work that was already enqueued.
        """
        first = self._queue.get()
        if first is None:
            return None
        batch = [first]
        while len(batch) < _MAX_BATCH_SIZE:
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                break
            if job is None:
                self._queue.put_nowait(None)  # let the outer loop see it and stop
                break
            batch.append(job)
        return batch

    def _write_batch(self, conn: sqlite3.Connection, batch: list[_WriteJob]) -> None:
        """Insert a batch inside one transaction, falling back to per-row on failure.

        A single malformed row would otherwise roll back every good row batched with it. On error
        the batch is retried one row at a time so exactly the bad row is dropped and logged, which
        is the behavior callers had before batching was introduced.
        """
        try:
            for table, row in batch:
                self._insert(conn, table, row)
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            for table, row in batch:
                self._write_row(conn, table, row)

    def _write_row(self, conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
        """Insert and commit one row, logging and dropping it if the insert fails."""
        try:
            self._insert(conn, table, row)
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            logger.exception("telemetry write to %s failed, row dropped: %r", table, row)

    def _upsert_order(self, conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        """Write an order's final state, keyed on `client_order_id`. No commit; may raise.

        `ON CONFLICT (client_order_id) DO UPDATE` rather than a bare UPDATE, because the pending
        row this supersedes is enqueued non-blocking and can be dropped under saturation. A bare
        UPDATE would then match zero rows and the order, a real position and real money, would be
        absent from the audit trail with nothing to show it had ever existed. That is precisely
        the failure recorded in `docs/GUIDE.md` section 6.

        `requested_at_ms` is deliberately not overwritten on conflict: the pending row stamped
        it before the request left, which is the more accurate answer to "when did the bot decide
        to place this", and it is what makes a stale `pending` row datable.
        """
        columns = _TABLE_COLUMNS["orders_fired"]
        correlation_id = row.get("correlation_id")
        if correlation_id is not None:
            conn.execute(
                "INSERT OR IGNORE INTO correlations (correlation_id, created_at_ms) VALUES (?, ?)",
                (correlation_id, _now_ms()),
            )
        column_list = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        updatable = [c for c in columns if c not in ("client_order_id", "requested_at_ms")]
        assignments = ", ".join(f"{c} = excluded.{c}" for c in updatable)
        conn.execute(
            f"INSERT INTO orders_fired ({column_list}) VALUES ({placeholders}) "
            f"ON CONFLICT (client_order_id) DO UPDATE SET {assignments}",
            tuple(row.get(column) for column in columns),
        )

    def _insert(self, conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
        """Insert one row without committing. Raises `sqlite3.Error` on failure."""
        if table == ORDER_RESOLUTION_JOB:
            self._upsert_order(conn, row)
            return
        columns = _TABLE_COLUMNS[table]
        correlation_id = row.get("correlation_id")
        if correlation_id is not None:
            conn.execute(
                "INSERT OR IGNORE INTO correlations (correlation_id, created_at_ms) VALUES (?, ?)",
                (correlation_id, _now_ms()),
            )
        column_list = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        # Current-state tables (heartbeats) overwrite their primary-keyed row in place; history
        # tables append.
        if table in _REPLACE_TABLES:
            verb = "INSERT OR REPLACE"
        elif table in _IGNORE_TABLES:
            verb = "INSERT OR IGNORE"
        else:
            verb = "INSERT"
        conn.execute(
            f"{verb} INTO {table} ({column_list}) VALUES ({placeholders})",
            tuple(row.get(column) for column in columns),
        )
