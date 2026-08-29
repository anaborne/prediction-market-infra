"""Age-based pruning for the telemetry database.

Every table grows monotonically otherwise, which is fine for an afternoon and not fine for a
multi-day unattended run. The retention periods below are set by what each table is *for*, not by
a uniform policy:

- `orders_fired` is kept forever. It is the record of what this bot did with money, and it is
  what `execution.risk`'s warm start reconstructs its counters from after a restart. Deleting it
  would silently reset those limits.
- `latency_events` (30 days) backs the detect→fire distribution, which needs enough history to
  compare one run against another.
- `decision_results` (14 days) is strategy-tuning data; older rows describe a model that has since
  changed.
- `market_snapshots` and `index_observations` (7 days) are the highest-volume tables by a wide
  margin and the least useful in hindsight.

`correlations` is a parent table with foreign keys pointing at it, so it is swept last and only
for rows nothing references any more. A row referenced by a kept `orders_fired` row survives
regardless of age.

Deletes are chunked. A single unbounded `DELETE` on a multi-day table takes one long write lock,
which under WAL blocks the writer thread this database exists to keep unblocked. Chunking also
keeps each transaction small enough that an interrupted prune loses nothing but its last chunk.
Note that `DELETE ... LIMIT` requires a compile option CPython's bundled SQLite does not ship, so
the chunking goes through a subquery on the primary key.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

_CHUNK_SIZE: Final = 5_000

# Table -> (timestamp column, retention in days). The timestamp column is indexed in every case
# (see `schema.sql`), so the chunk subquery is an index range scan rather than a table scan.
# `decision_results` is kept longer than the market tape deliberately: it is the input to
# `scripts/score_decisions.py`'s edge evaluation, which runs *after* a soak plus however long
# settlement data takes to accumulate. Pruning it at 14 days once deleted the very rows the
# soak existed to produce, before anything had consumed them.
_RETENTION: Final[dict[str, tuple[str, int]]] = {
    "latency_events": ("started_at_ms", 30),
    "decision_results": ("ts_ms", 30),
    "market_snapshots": ("observed_at_ms", 14),
    "index_observations": ("observed_at_ms", 14),
}

_CHILD_TABLES: Final[tuple[str, ...]] = (
    "orders_fired",
    "latency_events",
    "decision_results",
    "market_snapshots",
    "index_observations",
)


@dataclass(frozen=True, slots=True)
class PruneResult:
    """How many rows a prune removed, per table.

    Attributes:
        deleted: Rows deleted, keyed by table name. Tables with nothing to delete are present
            with a count of zero, so a caller logging this sees the full picture rather than
            inferring absence.
    """

    deleted: dict[str, int]

    @property
    def total(self) -> int:
        """Total rows deleted across all tables."""
        return sum(self.deleted.values())


def prune(conn: sqlite3.Connection, now_ms: int, *, checkpoint: bool = True) -> PruneResult:
    """Delete rows past their retention period, oldest first, in bounded chunks.

    Args:
        conn: A writable connection to the telemetry database.
        now_ms: Current Unix epoch milliseconds; ages are measured against this.
        checkpoint: Whether to run `PRAGMA wal_checkpoint(TRUNCATE)` afterwards. Deleting rows
            grows the WAL instead of shrinking the database, so without this a prune makes disk
            usage temporarily *worse*, which is the opposite of the point. Disabled only by
            tests that assert on delete counts alone.

    Returns:
        A `PruneResult` with per-table counts.
    """
    deleted: dict[str, int] = {}
    for table, (column, days) in _RETENTION.items():
        cutoff = now_ms - days * 86_400_000
        deleted[table] = _delete_older_than(conn, table, column, cutoff)
    deleted["correlations"] = _delete_orphan_correlations(conn)

    result = PruneResult(deleted=deleted)
    if result.total:
        logger.info("telemetry prune removed %d rows: %s", result.total, result.deleted)
    if checkpoint:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return result


def _delete_older_than(conn: sqlite3.Connection, table: str, column: str, cutoff_ms: int) -> int:
    """Chunk-delete rows in `table` whose `column` predates `cutoff_ms`. Returns rows deleted."""
    total = 0
    while True:
        cursor = conn.execute(
            f"DELETE FROM {table} WHERE id IN ("  # noqa: S608 - table/column are module constants
            f"  SELECT id FROM {table} WHERE {column} < ? ORDER BY {column} LIMIT {_CHUNK_SIZE}"
            f")",
            (cutoff_ms,),
        )
        conn.commit()
        if not cursor.rowcount:
            return total
        total += cursor.rowcount


def _delete_orphan_correlations(conn: sqlite3.Connection) -> int:
    """Delete `correlations` rows that nothing references any more.

    Swept by reachability rather than by age: a correlation belonging to an `orders_fired` row is
    still live no matter how old it is, because that table is never pruned. Deleting it would
    violate the foreign key and abort the transaction.
    """
    not_exists = " AND ".join(
        f"NOT EXISTS (SELECT 1 FROM {child} WHERE {child}.correlation_id = c.correlation_id)"
        for child in _CHILD_TABLES
    )
    total = 0
    while True:
        cursor = conn.execute(
            "DELETE FROM correlations WHERE correlation_id IN ("  # noqa: S608 - constants only
            f"  SELECT c.correlation_id FROM correlations c WHERE {not_exists} LIMIT {_CHUNK_SIZE}"
            ")"
        )
        conn.commit()
        if not cursor.rowcount:
            return total
        total += cursor.rowcount


def open_for_prune(db_path: Path) -> sqlite3.Connection:
    """Open a write connection configured the way a prune needs it.

    Foreign keys are enforced so an orphan sweep that would break a reference fails loudly here
    rather than corrupting the invariant silently, and `busy_timeout` is set because the hot-path
    writer holds this same file open.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def prune_now(db_path: Path, now_ms: int | None = None) -> PruneResult:
    """One complete prune against `db_path`: open, prune, checkpoint, close.

    The synchronous unit behind both `scripts/prune_telemetry.py` and `prune_periodically()`.
    """
    conn = open_for_prune(db_path)
    try:
        return prune(conn, now_ms if now_ms is not None else int(time.time() * 1000))
    finally:
        conn.close()


async def prune_periodically(db_path: Path, interval_seconds: float = 86_400.0) -> None:
    """Run `prune_now` every `interval_seconds`, forever, off the event loop.

    Scheduled retention for unattended runs. Before this, pruning was a manual script an
    operator had to remember, which a 72-hour soak will not forgive. Hosted by the executor
    process (the least busy of the three, and already a writer to this database); the prune
    itself runs in a worker thread via `asyncio.to_thread`, so the chunked deletes never occupy
    the event loop, and the writer thread's `busy_timeout` covers the brief per-chunk locks.

    The first prune happens after one full interval and never at startup, since startup is
    already the busiest moment, and `scripts/prune_telemetry.py` exists for an operator who wants
    one now.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            result = await asyncio.to_thread(prune_now, db_path)
            logger.info("scheduled telemetry prune complete: %d rows removed", result.total)
        except Exception:
            # A failed prune is a disk-usage problem and no trading problem, so log and try again
            # next interval instead of taking the process down over housekeeping.
            logger.exception("scheduled telemetry prune failed; retrying next interval")
