"""Tests for `execution.risk`.

`check()`/`record_*` are pure in-memory functions driven with explicit `now_ms` values, with no
clocks and no sleeps. `warm_start()` is tested against a real SQLite file built by `TelemetryDB`
(the same schema production writes), because the whole point of the warm start is that a restart
reconstructs the same state the crashed process held.
"""

from __future__ import annotations

from pathlib import Path

from kalshi_bot.execution.risk import RiskGate, RiskLimits, event_ticker_of
from kalshi_bot.telemetry.db import TelemetryDB

_NOW_MS = 1_787_400_000_000


def _limits(**overrides: object) -> RiskLimits:
    base: dict[str, object] = {
        "allowed_exchange_indexes": frozenset({2}),
        "max_attempts_per_hour": 3,
        "max_attempts_per_day": 5,
        "max_attempts_per_ticker_per_day": 2,
        "max_notional_per_day_dollars": 10.0,
        "max_concurrent_dispatches": 1,
        "max_position_contracts_per_ticker": 3.0,
        "max_attempts_per_correlation_group_per_day": 4,
        "max_position_contracts_per_correlation_group": 6.0,
        "refire_cooldown_seconds": 60.0,
    }
    base.update(overrides)
    return RiskLimits(**base)  # type: ignore[arg-type]


def _check(gate: RiskGate, **overrides: object) -> str | None:
    kwargs: dict[str, object] = {
        "ticker": "KXBTCD-T100",
        "direction": "yes",
        "exchange_index": 2,
        "count": 1,
        "price_dollars": 0.50,
        "now_ms": _NOW_MS,
    }
    kwargs.update(overrides)
    return gate.check(**kwargs)  # type: ignore[arg-type]


def test_allows_a_first_fire_within_all_limits() -> None:
    gate = RiskGate(_limits())

    assert _check(gate) is None


def test_rejects_a_shard_outside_the_allowlist() -> None:
    gate = RiskGate(_limits())

    rejection = _check(gate, exchange_index=0)

    assert rejection is not None
    assert "exchange_index=0" in rejection


def test_rejects_an_unknown_shard() -> None:
    """`-1` means the poller did not know the shard; unknown cannot be verified as funded."""
    gate = RiskGate(_limits())

    rejection = _check(gate, exchange_index=-1)

    assert rejection is not None
    assert "cannot be verified as funded" in rejection


def test_cooldown_blocks_the_same_ticker_direction_then_releases() -> None:
    gate = RiskGate(_limits(refire_cooldown_seconds=60.0))
    gate.record_attempt("KXBTCD-T100", "yes", 1, 0.50, _NOW_MS)

    blocked = _check(gate, now_ms=_NOW_MS + 30_000)
    released = _check(gate, now_ms=_NOW_MS + 61_000)

    assert blocked is not None and "cooldown" in blocked
    assert released is None


def test_cooldown_is_per_ticker_and_direction() -> None:
    gate = RiskGate(_limits())
    gate.record_attempt("KXBTCD-T100", "yes", 1, 0.50, _NOW_MS)

    # Same ticker, other direction, so this cooldown does not block it.
    assert _check(gate, direction="no", now_ms=_NOW_MS + 1_000) is None
    # Other ticker, same direction, likewise.
    assert _check(gate, ticker="KXBTCD-T110", now_ms=_NOW_MS + 1_000) is None


def test_hourly_attempt_cap() -> None:
    gate = RiskGate(_limits(max_attempts_per_hour=2, max_attempts_per_ticker_per_day=99))
    gate.record_attempt("KXBTCD-T90", "yes", 1, 0.10, _NOW_MS - 300_000)
    gate.record_attempt("KXBTCD-T95", "yes", 1, 0.10, _NOW_MS - 200_000)

    rejection = _check(gate)

    assert rejection is not None and "attempts in the last hour" in rejection
    # The same attempts aged past the hour no longer count against the hourly cap.
    assert _check(gate, now_ms=_NOW_MS + 3_600_001) is None


def test_daily_attempt_cap_outlives_the_hourly_window() -> None:
    gate = RiskGate(
        _limits(
            max_attempts_per_hour=99, max_attempts_per_day=2, max_attempts_per_ticker_per_day=99
        )
    )
    gate.record_attempt("KXBTCD-T90", "yes", 1, 0.10, _NOW_MS - 10 * 3_600_000)
    gate.record_attempt("KXBTCD-T95", "yes", 1, 0.10, _NOW_MS - 5 * 3_600_000)

    rejection = _check(gate)

    assert rejection is not None and "attempts in the last 24h" in rejection


def test_per_ticker_daily_cap() -> None:
    gate = RiskGate(_limits(max_attempts_per_ticker_per_day=1, max_attempts_per_hour=99))
    gate.record_attempt("KXBTCD-T100", "no", 1, 0.10, _NOW_MS - 2 * 3_600_000)

    same_ticker = _check(gate)  # different direction, same ticker, still capped
    other_ticker = _check(gate, ticker="KXBTCD-T110")

    assert same_ticker is not None and "KXBTCD-T100" in same_ticker
    assert other_ticker is None


def test_daily_notional_cap_counts_the_candidate_order() -> None:
    gate = RiskGate(_limits(max_notional_per_day_dollars=1.0))
    gate.record_attempt("KXBTCD-T90", "yes", 1, 0.60, _NOW_MS - 3_600_000)

    rejection = _check(gate, count=1, price_dollars=0.50)  # 0.60 + 0.50 > 1.00

    assert rejection is not None and "notional" in rejection


def test_concurrency_cap_follows_begin_and_end() -> None:
    gate = RiskGate(_limits(max_concurrent_dispatches=1))

    gate.begin_dispatch()
    blocked = _check(gate)
    gate.end_dispatch()
    released = _check(gate)

    assert blocked is not None and "in flight" in blocked
    assert released is None


def test_position_cap_uses_recorded_fills() -> None:
    gate = RiskGate(_limits(max_position_contracts_per_ticker=3.0))
    gate.record_fill("KXBTCD-T100", 3.0, _NOW_MS)

    same_ticker = _check(gate)
    other_ticker = _check(gate, ticker="KXBTCD-T110")

    assert same_ticker is not None and "position" in same_ticker
    assert other_ticker is None


def test_position_is_gross_not_net() -> None:
    """5 YES and 5 NO of one strike is 10 contracts at risk of being wrong, not 0."""
    gate = RiskGate(_limits(max_position_contracts_per_ticker=3.0))
    gate.record_fill("KXBTCD-T100", 2.0, _NOW_MS)  # a yes fill
    gate.record_fill("KXBTCD-T100", 1.0, _NOW_MS)  # a no fill, still adds

    assert _check(gate) is not None


def test_position_cap_ages_fills_out_of_the_24h_window() -> None:
    """A fill from a long-settled strike must not count against the cap forever.

    Regression test: `_position_by_ticker`/`_position_by_correlation_group` used to be
    accumulate-only with no decrement path (no close, cancel, expiry, or settlement ever
    subtracted from them), so a long-running process would eventually reject every real fire in
    a ticker/group regardless of what was actually still open on Kalshi.
    """
    gate = RiskGate(_limits(max_position_contracts_per_ticker=3.0))
    gate.record_fill("KXBTCD-T100", 3.0, _NOW_MS)

    still_within_window = _check(gate, now_ms=_NOW_MS + 3_600_000)
    aged_out = _check(gate, now_ms=_NOW_MS + 86_400_000 + 1)  # just past 24h

    assert still_within_window is not None and "position" in still_within_window
    assert aged_out is None


def test_correlation_group_position_cap_ages_fills_out_of_the_24h_window() -> None:
    gate = RiskGate(_limits(max_position_contracts_per_correlation_group=3.0))
    gate.record_attempt("KXBTCD-T100", "yes", 1, 0.50, _NOW_MS, correlation_group="majors")
    gate.record_fill("KXBTCD-T100", 3.0, _NOW_MS)

    aged_out = _check(
        gate,
        ticker="KXBTC15M-T100",
        correlation_group="majors",
        now_ms=_NOW_MS + 86_400_000 + 1,  # just past 24h
    )

    assert aged_out is None


def test_correlation_group_attempt_cap_spans_distinct_tickers() -> None:
    """Two tickers sharing a group (e.g. an hourly series and its 15-minute counterpart) must
    not each get independent per-ticker headroom. The group cap sees them as one exposure."""
    gate = RiskGate(_limits(max_attempts_per_correlation_group_per_day=2, max_attempts_per_hour=99))
    gate.record_attempt("KXBTCD-T100", "yes", 1, 0.50, _NOW_MS, correlation_group="majors")
    gate.record_attempt("KXBTC15M-T100", "yes", 1, 0.50, _NOW_MS, correlation_group="majors")

    same_group = _check(gate, ticker="KXETHD-T200", correlation_group="majors")
    other_group = _check(gate, ticker="KXETHD-T200", correlation_group="altcoins")
    ungrouped = _check(gate, ticker="KXETHD-T200")

    assert same_group is not None and "majors" in same_group
    assert other_group is None
    assert ungrouped is None  # "" means unknown, so the group cap is skipped and not applied as 0


def test_correlation_group_position_cap_spans_distinct_tickers() -> None:
    gate = RiskGate(_limits(max_position_contracts_per_correlation_group=3.0))
    gate.record_attempt("KXBTCD-T100", "yes", 1, 0.50, _NOW_MS, correlation_group="majors")
    gate.record_fill("KXBTCD-T100", 3.0, _NOW_MS)

    same_group = _check(gate, ticker="KXBTC15M-T100", correlation_group="majors")
    other_group = _check(gate, ticker="KXBTC15M-T100", correlation_group="altcoins")

    assert same_group is not None and "correlation group" in same_group
    assert other_group is None


def test_reconcile_clears_a_ticker_that_is_no_longer_actually_open() -> None:
    """The regression this module exists to prevent: Kalshi says nothing is open, so the cap
    must not keep rejecting real fires against a fill-accumulation ledger that never forgets."""
    gate = RiskGate(_limits(max_position_contracts_per_ticker=3.0))
    gate.record_fill("KXBTCD-T100", 3.0, _NOW_MS)
    assert _check(gate, now_ms=_NOW_MS + 1_000) is not None  # capped, per the stale fill

    gate.reconcile_open_positions({}, snapshot_at_ms=_NOW_MS + 2_000)

    assert _check(gate, now_ms=_NOW_MS + 3_000) is None


def test_reconcile_clears_a_correlation_group_that_is_no_longer_actually_open() -> None:
    gate = RiskGate(_limits(max_position_contracts_per_correlation_group=3.0))
    gate.record_attempt("KXBTCD-T100", "yes", 1, 0.50, _NOW_MS, correlation_group="majors")
    gate.record_fill("KXBTCD-T100", 3.0, _NOW_MS)
    blocked = _check(
        gate, ticker="KXBTC15M-T100", correlation_group="majors", now_ms=_NOW_MS + 1_000
    )
    assert blocked is not None

    gate.reconcile_open_positions({}, snapshot_at_ms=_NOW_MS + 2_000)

    released = _check(
        gate, ticker="KXBTC15M-T100", correlation_group="majors", now_ms=_NOW_MS + 3_000
    )
    assert released is None


def test_reconcile_uses_live_open_contracts_as_the_new_baseline() -> None:
    gate = RiskGate(_limits(max_position_contracts_per_ticker=5.0))

    gate.reconcile_open_positions({"KXBTCD-T100": 5.0}, snapshot_at_ms=_NOW_MS)

    rejection = _check(gate, now_ms=_NOW_MS + 1_000)
    assert rejection is not None and "5" in rejection


def test_reconcile_does_not_drop_a_fill_recorded_after_the_snapshot_was_taken() -> None:
    """A fire dispatched during or after the live fetch may not be reflected in its result yet,
    so it must stay counted instead of being wiped out by a reconciliation that predates it."""
    gate = RiskGate(_limits(max_position_contracts_per_ticker=3.0))

    gate.reconcile_open_positions({}, snapshot_at_ms=_NOW_MS)
    gate.record_fill("KXBTCD-T100", 3.0, _NOW_MS + 500)  # fired just after the snapshot was taken

    assert _check(gate, now_ms=_NOW_MS + 1_000) is not None


def test_reconcile_correlation_group_sums_every_open_ticker_in_the_group() -> None:
    gate = RiskGate(_limits(max_position_contracts_per_correlation_group=5.0))
    gate.record_attempt("KXBTCD-T100", "yes", 1, 0.50, _NOW_MS, correlation_group="majors")
    gate.record_attempt("KXETHD-T200", "yes", 1, 0.50, _NOW_MS, correlation_group="majors")

    gate.reconcile_open_positions({"KXBTCD-T100": 3.0, "KXETHD-T200": 3.0}, snapshot_at_ms=_NOW_MS)

    rejection = _check(
        gate, ticker="KXBTC15M-T100", correlation_group="majors", now_ms=_NOW_MS + 1_000
    )
    assert rejection is not None and "correlation group" in rejection


def _write_order(db: TelemetryDB, **overrides: object) -> None:
    row: dict[str, object] = {
        "correlation_id": "corr-ws",
        "client_order_id": (
            f"client-{overrides.get('requested_at_ms', 0)}-{overrides.get('ticker')}"
        ),
        "ticker": "KXBTCD-T100",
        "outcome_side": "yes",
        "count": "1.00",
        "price_dollars": "0.5000",
        "time_in_force": "fill_or_kill",
        "self_trade_prevention_type": "taker_at_cross",
        "status": "filled",
        "fill_count": "1.00",
        "requested_at_ms": _NOW_MS - 1_000,
    }
    row.update(overrides)
    db.record_order_fired(row)


def test_warm_start_restores_cooldown_positions_and_attempts(tmp_path: Path) -> None:
    """The restart-surviving cooldown: the reason this gate exists in this shape.

    The decision engine's cooldown is in-memory, so a crash loop re-fires the same edge as fast
    as the supervisor restarts the process. The gate rebuilt from `orders_fired` must block the
    re-fire exactly as the crashed process would have.
    """
    db = TelemetryDB(tmp_path / "telemetry.db")
    db.initialize()
    _write_order(db, requested_at_ms=_NOW_MS - 10_000, fill_count="2.00")
    db.close()

    gate = RiskGate.warm_start(_limits(), tmp_path / "telemetry.db", now_ms=_NOW_MS)

    # 10 s since the last attempt, cooldown 60 s: the restarted process still refuses.
    blocked = _check(gate)
    assert blocked is not None and "cooldown" in blocked

    # The fill it recorded still counts toward the position cap.
    after_cooldown = _check(gate, now_ms=_NOW_MS + 61_000, count=2)
    assert after_cooldown is not None and "position" in after_cooldown


def test_warm_start_ignores_rejected_rows(tmp_path: Path) -> None:
    """A refusal is not an attempt, and counting it would lock the gate on its own output."""
    db = TelemetryDB(tmp_path / "telemetry.db")
    db.initialize()
    _write_order(
        db,
        requested_at_ms=_NOW_MS - 10_000,
        status="rejected",
        fill_count=None,
        error_message="risk gate: cooldown",
    )
    db.close()

    gate = RiskGate.warm_start(_limits(), tmp_path / "telemetry.db", now_ms=_NOW_MS)

    assert _check(gate) is None


def test_warm_start_restores_correlation_group_attempts_and_position(tmp_path: Path) -> None:
    """The group cap must survive a restart the same way the per-ticker caps already do."""
    db = TelemetryDB(tmp_path / "telemetry.db")
    db.initialize()
    _write_order(
        db,
        ticker="KXBTCD-T100",
        correlation_group="majors",
        requested_at_ms=_NOW_MS - 10_000,
        fill_count="2.00",
    )
    db.close()

    gate = RiskGate.warm_start(
        _limits(max_attempts_per_correlation_group_per_day=1),
        tmp_path / "telemetry.db",
        now_ms=_NOW_MS,
    )

    rejection = _check(
        gate, ticker="KXETHD-T200", correlation_group="majors", now_ms=_NOW_MS + 61_000
    )

    assert rejection is not None and "majors" in rejection

    # The warm-started fill counts toward the group's position cap too.
    gate = RiskGate.warm_start(
        _limits(max_position_contracts_per_correlation_group=1.0),
        tmp_path / "telemetry.db",
        now_ms=_NOW_MS,
    )
    position_rejection = _check(
        gate, ticker="KXETHD-T200", correlation_group="majors", now_ms=_NOW_MS + 61_000
    )
    assert position_rejection is not None and "correlation group" in position_rejection


def test_warm_start_row_without_a_correlation_group_does_not_join_any_group(
    tmp_path: Path,
) -> None:
    """A row predating this field (NULL) must not silently join the `""`/unknown bucket."""
    db = TelemetryDB(tmp_path / "telemetry.db")
    db.initialize()
    _write_order(db, ticker="KXBTCD-T100", requested_at_ms=_NOW_MS - 10_000, fill_count="2.00")
    db.close()

    gate = RiskGate.warm_start(
        _limits(max_attempts_per_correlation_group_per_day=1),
        tmp_path / "telemetry.db",
        now_ms=_NOW_MS,
    )

    assert (
        _check(gate, ticker="KXETHD-T200", correlation_group="majors", now_ms=_NOW_MS + 61_000)
        is None
    )


def test_warm_start_ignores_rows_older_than_a_day(tmp_path: Path) -> None:
    db = TelemetryDB(tmp_path / "telemetry.db")
    db.initialize()
    _write_order(db, requested_at_ms=_NOW_MS - 90_000_000)  # ~25h ago
    db.close()

    gate = RiskGate.warm_start(_limits(), tmp_path / "telemetry.db", now_ms=_NOW_MS)

    assert _check(gate) is None


# Every strike of an event resolves off one number, so a ladder-wide sweep is one bet levered N
# ways, not N positions. The per-ticker cap counts them as independent and the correlation-group
# cap only sees them pooled with everything else; neither bounds a single directional view.


def test_event_ticker_is_derived_from_the_strike_suffix() -> None:
    assert event_ticker_of("KXBTCD-26AUG2118-T78399.99") == "KXBTCD-26AUG2118"
    assert event_ticker_of("KXBTC15M-26AUG221800-00") == "KXBTC15M-26AUG221800"


def test_event_ticker_of_a_bare_ticker_is_itself() -> None:
    assert event_ticker_of("KXBTCD") == "KXBTCD"
    assert event_ticker_of("") == ""


def test_ladder_sweep_of_one_event_is_capped() -> None:
    # The 2026-08-21 shape: ten consecutive strikes of one event, each individually under the
    # per-ticker cap, together a single oversized directional bet.
    limits = RiskLimits(
        allowed_exchange_indexes=frozenset({0}),
        max_attempts_per_hour=100,
        max_attempts_per_day=100,
        max_attempts_per_ticker_per_day=100,
        max_notional_per_day_dollars=10_000.0,
        max_position_contracts_per_ticker=100.0,
        max_attempts_per_correlation_group_per_day=100,
        max_position_contracts_per_correlation_group=100.0,
        max_attempts_per_event_per_day=100,
        max_position_contracts_per_event=6.0,
        refire_cooldown_seconds=0.0,
    )
    gate = RiskGate(limits)
    now = 1_700_000_000_000
    accepted = 0
    for index in range(10):
        ticker = f"KXBTCD-26AUG2118-T{78000 + index * 100}.99"
        if gate.check(ticker, "yes", 0, 1, 0.5, now) is None:
            accepted += 1
            gate.record_fill(ticker, 1.0, now)
    assert accepted == 6, "the per-event cap must bound the whole ladder, not each strike"


def test_per_event_attempt_cap_limits_sweeps_of_one_settlement() -> None:
    limits = RiskLimits(
        allowed_exchange_indexes=frozenset({0}),
        max_attempts_per_hour=100,
        max_attempts_per_day=100,
        max_attempts_per_ticker_per_day=100,
        max_notional_per_day_dollars=10_000.0,
        max_position_contracts_per_ticker=100.0,
        max_attempts_per_correlation_group_per_day=100,
        max_position_contracts_per_correlation_group=100.0,
        max_attempts_per_event_per_day=3,
        max_position_contracts_per_event=100.0,
        refire_cooldown_seconds=0.0,
    )
    gate = RiskGate(limits)
    now = 1_700_000_000_000
    for index in range(3):
        ticker = f"KXBTCD-26AUG2118-T{78000 + index * 100}.99"
        assert gate.check(ticker, "yes", 0, 1, 0.5, now) is None
        gate.record_attempt(ticker, "yes", 1, 0.5, now)
    refusal = gate.check("KXBTCD-26AUG2118-T78999.99", "yes", 0, 1, 0.5, now)
    assert refusal is not None
    assert "event" in refusal


def test_a_different_event_is_unaffected_by_another_events_cap() -> None:
    limits = RiskLimits(
        allowed_exchange_indexes=frozenset({0}),
        max_attempts_per_hour=100,
        max_attempts_per_day=100,
        max_attempts_per_ticker_per_day=100,
        max_notional_per_day_dollars=10_000.0,
        max_position_contracts_per_ticker=100.0,
        max_attempts_per_correlation_group_per_day=100,
        max_position_contracts_per_correlation_group=100.0,
        max_attempts_per_event_per_day=100,
        max_position_contracts_per_event=2.0,
        refire_cooldown_seconds=0.0,
    )
    gate = RiskGate(limits)
    now = 1_700_000_000_000
    for index in range(2):
        ticker = f"KXBTCD-26AUG2118-T{78000 + index * 100}.99"
        assert gate.check(ticker, "yes", 0, 1, 0.5, now) is None
        gate.record_fill(ticker, 1.0, now)
    assert gate.check("KXBTCD-26AUG2118-T78900.99", "yes", 0, 1, 0.5, now) is not None
    # A different settlement is a genuinely separate bet and must still be allowed.
    assert gate.check("KXBTCD-26AUG2119-T78000.99", "yes", 0, 1, 0.5, now) is None


def test_reconciled_positions_are_aggregated_by_event() -> None:
    limits = RiskLimits(
        allowed_exchange_indexes=frozenset({0}),
        max_attempts_per_hour=100,
        max_attempts_per_day=100,
        max_attempts_per_ticker_per_day=100,
        max_notional_per_day_dollars=10_000.0,
        max_position_contracts_per_ticker=100.0,
        max_attempts_per_correlation_group_per_day=100,
        max_position_contracts_per_correlation_group=100.0,
        max_attempts_per_event_per_day=100,
        max_position_contracts_per_event=5.0,
        refire_cooldown_seconds=0.0,
    )
    gate = RiskGate(limits)
    now = 1_700_000_000_000
    gate.reconcile_open_positions(
        {
            "KXBTCD-26AUG2118-T78000.99": 2.0,
            "KXBTCD-26AUG2118-T78100.99": 2.0,
        },
        now,
    )
    # 4 already open across the event; a 2-contract order would reach 6, past the cap of 5.
    refusal = gate.check("KXBTCD-26AUG2118-T78200.99", "yes", 0, 2, 0.5, now)
    assert refusal is not None
    assert "event" in refusal
    assert gate.check("KXBTCD-26AUG2118-T78200.99", "yes", 0, 1, 0.5, now) is None
