"""The risk gate: the last check between a wake message and a real order.

Sited in the executor, immediately before dispatch, because the executor is the only chokepoint
that still works when the poller is wedged. A poller bug that floods wakes, a crash loop that
re-fires the same edge, or a clock problem that defeats the engine's in-memory cooldown all
arrive here, and here is where they stop.

Two design rules, both load-bearing:

- `check()` and the `record_*`/`reconcile_open_positions()` methods do no I/O. `ENGINEERING.md`
  rule 1: state is pre-computed or refreshed in the background, never queried at fire time. The
  one database read happens once, in `warm_start()`, before the executor accepts its first wake.
  Every fire-time decision is arithmetic over in-memory state, and `reconcile_open_positions()`
  is handed already-fetched data by its caller instead of fetching anything itself.
- State survives restart. The decision engine's re-fire cooldown is in-memory, so a crash
  loop re-fires the same edge as fast as the supervisor restarts the process. `warm_start()`
  rebuilds the last 24 hours of attempts, notional, and cooldowns from `orders_fired`, which is
  also why that table is kept forever and why rejected rows are excluded from it here (a refusal
  is not an attempt; counting it would let a rejection storm lock the gate shut on its own
  output).

Position caps track live Kalshi truth, not a fill-accumulation heuristic. An earlier version
of this module summed every real fill's `fill_count` into `_position_by_ticker`/
`_position_by_correlation_group` and never subtracted anything back out, with no decrement on
close, cancel, expiry, or settlement, so the totals only ever grew for as long as the process stayed
up, eventually rejecting every real fire in a group regardless of what was actually still open
on Kalshi. A once-only-at-`warm_start()` 24-hour filter papered over this in the common case (a
process that restarts at least daily), but a long-running soak has no such reset: 301 real fills
totaling 10,005 contracts inside one 65-minute window early in a soak was enough to lock the
`majors` group's cap for the following 23 hours straight, with zero actual open positions the
entire time (2026-08-22 incident).

The fix is `reconcile_open_positions()`: `execution.account_monitor`'s existing 30-second
`/portfolio/positions` poll (already running in this process for the dashboard, so no new API
calls) now also hands its result here. Kalshi removes a ticker from `/portfolio/positions` the
moment it fully settles, so absence *is* the "this is closed" signal, and never an assumption
about how long settlement usually takes. Between polls (up to 30s), `record_fill()` still layers a
just-dispatched fire's `fill_count` on top of the last reconciled baseline, exactly as before, so
a fire the next instant still counts before the following poll confirms it; fills already covered
by a later reconciliation are excluded from the live count rather than double-added. Before the
very first reconciliation completes (briefly, at boot), position checks fall back to the same
rolling 24-hour fill window this module used exclusively before. See `_position_for_ticker`.

One deliberate semantic change worth being explicit about: `/portfolio/positions` reports
`position_fp`, a net signed count (positive = net long YES, negative = net long NO), and Kalshi
does not expose separate per-side counts there the way `/portfolio/settlements` does. The
fill-accumulation ledger used to track gross contracts per ticker deliberately ("a bot
holding 5 YES and 5 NO of one strike has 10 contracts at risk of being wrong, not 0"). Live
reconciliation can only see the net `|position_fp|`, so a ticker simultaneously holding opposing
positions would now read as smaller exposure than gross accounting reported. This strategy fires
a single FOK order on one side per detected edge and is not expected to intentionally hold both
sides of one ticker; net exposure is also arguably the more economically correct thing to cap
directly against, since a hedged position carries less real capital risk than the gross count
suggests. If this bot's behavior ever changes to intentionally hedge a ticker, this tradeoff needs
revisiting. See `docs/GUIDE.md`.
"""

from __future__ import annotations

import sqlite3
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

_HOUR_MS: Final = 3_600_000
_DAY_MS: Final = 86_400_000


def event_ticker_of(market_ticker: str) -> str:
    """The event a market ticker belongs to, meaning every strike on one settlement of one
    underlying.

    Kalshi market tickers are `<EVENT>-<STRIKE>`: `KXBTCD-26AUG2118-T78399.99` belongs to event
    `KXBTCD-26AUG2118`, and `KXBTC15M-26AUG221800-00` to `KXBTC15M-26AUG221800`. Verified against
    the `event_ticker` field the API returns alongside each market and settlement, rather than
    inferred from the ticker grammar alone.

    This distinction is the one the risk gate was missing. Strikes on a single event are not
    independent positions. They are the same bet on the same number at different thresholds. In
    the 2026-08-21 blowup the bot bought YES across ten consecutive strikes of one BTC event at
    0.97, 0.97, 0.94, 0.88, 0.63, 0.45, 0.28, 0.18, 0.10 and 0.07, which the per-ticker cap saw
    as ten small positions and the correlation-group cap saw only in aggregate with everything
    else. It was one directional view, levered ten ways, and it lost `$1,608` on a single
    settlement.

    Args:
        market_ticker: A Kalshi market ticker.

    Returns:
        The event ticker, or the input unchanged when it carries no strike suffix (which is
        already an event ticker, or a malformed one, and either way grouping it with itself is the
        conservative reading).
    """
    if "-" not in market_ticker:
        return market_ticker
    return market_ticker.rsplit("-", 1)[0]


class SupportsRiskLimits(Protocol):
    """The `risk_*` attributes `RiskLimits.from_config` reads.

    A `Protocol` rather than `config.KalshiBotConfig` on purpose: `execution` must not depend on the
    config loader, and structural typing gives the factory its type safety without the import.
    Adding a cap to `RiskLimits` means adding it here too, and `tests/test_config.py` fails if the
    config side is missing.

    Declared as read-only properties rather than plain attributes: `KalshiBotConfig` is a frozen
    dataclass, and a Protocol member written as a bare annotation is a *settable* variable, which
    a frozen attribute does not satisfy.
    """

    @property
    def risk_max_attempts_per_hour(self) -> int: ...

    @property
    def risk_max_attempts_per_day(self) -> int: ...

    @property
    def risk_max_attempts_per_ticker_per_day(self) -> int: ...

    @property
    def risk_max_notional_per_day_dollars(self) -> float: ...

    @property
    def risk_max_concurrent_dispatches(self) -> int: ...

    @property
    def risk_max_position_contracts_per_ticker(self) -> float: ...

    @property
    def risk_max_attempts_per_correlation_group_per_day(self) -> int: ...

    @property
    def risk_max_position_contracts_per_correlation_group(self) -> float: ...

    @property
    def risk_max_attempts_per_event_per_day(self) -> int: ...

    @property
    def risk_max_position_contracts_per_event(self) -> float: ...

    @property
    def risk_refire_cooldown_seconds(self) -> float: ...


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Caps enforced by `RiskGate`, all applying to real fires only.

    Defaults are deliberately tight for a demo bot placing 1-contract FOK orders; they are
    code-tunable (same convention as `decision.runner.RunnerConfig`, see
    `docs/configuration.md`'s "Not configurable by environment variable" section) except for
    `allowed_exchange_indexes`, which has no default so the caller must wire it from config.

    Attributes:
        allowed_exchange_indexes: Shards a dispatch may target. A wake whose `exchange_index`
            is not in this set, including `-1`, meaning the poller did not know the shard, is
            rejected: an unknown shard cannot be verified as funded, and the shard gate
            requires the unfunded case to be refused, not attempted.
        max_attempts_per_hour: Wire attempts (dispatches, not rejections) in a rolling hour.
        max_attempts_per_day: Wire attempts in a rolling 24 hours.
        max_attempts_per_ticker_per_day: Wire attempts per market ticker in a rolling 24 hours.
        max_notional_per_day_dollars: Sum of `count × price` dispatched in a rolling 24 hours.
        max_concurrent_dispatches: Dispatches in flight at once. A stuck order path holds its
            slot until it resolves, which is the point.
        max_position_contracts_per_ticker: Open contracts on one ticker right now, per the latest
            `reconcile_open_positions()` call plus any not-yet-reconciled fills; a dispatch that
            could push past this is rejected before it is sent.
        max_attempts_per_correlation_group_per_day: Wire attempts across every ticker sharing an
            `AssetConfig.correlation_group` (e.g. an hourly series and its 15-minute counterpart
            on the same underlying), rolling 24 h. The per-ticker cap above cannot see this
            correlation, since two tickers never collide on it, so this is a genuinely additional
            constraint and no restatement of the per-ticker one. A wake carrying an unknown group
            (`""`, a pre-this-field frame) skips this check instead of guessing a group.
        max_position_contracts_per_correlation_group: Open contracts across every ticker sharing
            a correlation group right now. Same "unknown group skips the check" rule, same live
            reconciliation as the per-ticker cap.
        max_attempts_per_event_per_day: Wire attempts across every strike of one event (see
            `event_ticker_of`), rolling 24 h. Sits between the per-ticker and per-group caps,
            and catches what neither could: a ladder-wide sweep of one settlement, which the
            per-ticker cap counts as many small independent positions.
        max_position_contracts_per_event: Open contracts across every strike of one event right
            now. The binding constraint on a single directional view, since every strike of an event
            resolves off one number, so this is the cap that actually bounds the loss if that
            number goes the wrong way.
        refire_cooldown_seconds: Minimum time between wire attempts on the same
            `(ticker, direction)`. The restart-surviving backstop behind the engine's in-memory
            cooldown, deliberately longer than it.
    """

    allowed_exchange_indexes: frozenset[int]
    max_attempts_per_hour: int = 10
    max_attempts_per_day: int = 50
    max_attempts_per_ticker_per_day: int = 5
    max_notional_per_day_dollars: float = 50.0
    max_concurrent_dispatches: int = 2
    max_position_contracts_per_ticker: float = 5.0
    max_attempts_per_correlation_group_per_day: int = 10
    max_position_contracts_per_correlation_group: float = 10.0
    max_attempts_per_event_per_day: int = 6
    max_position_contracts_per_event: float = 6.0
    refire_cooldown_seconds: float = 60.0

    @classmethod
    def from_config(cls, config: SupportsRiskLimits, allowed: frozenset[int]) -> RiskLimits:
        """Build the limits from a config object, in one place a test can call.

        `scripts/run_executor.py` used to assemble this inline, which meant the only check that
        every cap actually reached the gate was a source-level one: a test that read the
        script and looked for keyword names. That caught a forgotten field but could never catch
        a field wired to the *wrong* config value, and two caps
        (`max_attempts_per_event_per_day`, `max_position_contracts_per_event`) had already gone
        unwired entirely, silently taking the dataclass defaults.

        `config` is typed as a `Protocol`, so `execution` still does not import the config
        *loader*, so the coupling this factory was previously rejected for is avoided by
        structural typing instead of by leaving the code untestable.

        Args:
            config: Anything carrying the `risk_*` attributes, in production a
                `config.KalshiBotConfig`.
            allowed: The shard allowlist. Separate because it is a correctness gate instead of a
                throttle, and comes from `KALSHI_ALLOWED_EXCHANGE_INDEXES` rather than a
                `KALSHI_RISK_*` variable.

        Returns:
            The populated limits.
        """
        return cls(
            allowed_exchange_indexes=allowed,
            max_attempts_per_hour=config.risk_max_attempts_per_hour,
            max_attempts_per_day=config.risk_max_attempts_per_day,
            max_attempts_per_ticker_per_day=config.risk_max_attempts_per_ticker_per_day,
            max_notional_per_day_dollars=config.risk_max_notional_per_day_dollars,
            max_concurrent_dispatches=config.risk_max_concurrent_dispatches,
            max_position_contracts_per_ticker=config.risk_max_position_contracts_per_ticker,
            max_attempts_per_correlation_group_per_day=(
                config.risk_max_attempts_per_correlation_group_per_day
            ),
            max_position_contracts_per_correlation_group=(
                config.risk_max_position_contracts_per_correlation_group
            ),
            max_attempts_per_event_per_day=config.risk_max_attempts_per_event_per_day,
            max_position_contracts_per_event=config.risk_max_position_contracts_per_event,
            refire_cooldown_seconds=config.risk_refire_cooldown_seconds,
        )


class RiskGate:
    """In-memory risk state with a one-time database warm start and periodic live reconciliation.

    Attributes:
        limits: The caps this gate enforces.
    """

    def __init__(self, limits: RiskLimits) -> None:
        """Build an empty gate (no history). Prefer `warm_start()` in production.

        Args:
            limits: The caps to enforce.
        """
        self.limits = limits
        # (requested_at_ms, ticker, correlation_group, notional_dollars) per wire attempt,
        # oldest first, pruned to the 24 h window on every check. Bounded by max_attempts_per_day,
        # so scans are trivial. `correlation_group` is `""` for a wake that did not carry one.
        self._attempts: deque[tuple[int, str, str, float]] = deque()
        self._last_attempt_ms: dict[tuple[str, str], int] = {}
        # (recorded_at_ms, ticker, correlation_group, fill_count) per fill not yet superseded by
        # a `reconcile_open_positions()` call, oldest first, pruned to 24h as a memory bound (see
        # `_prune`). `_position_for_ticker`/`_position_for_group` sum whichever of these are newer
        # than the last reconciliation on top of the reconciled baseline below.
        self._fills: deque[tuple[int, str, str, float]] = deque()
        # A ticker's group rarely if ever changes, but `record_fill(ticker, ...)` and
        # `reconcile_open_positions(open_positions, ...)` only get a ticker and no group, so this is
        # populated in `record_attempt()`/`warm_start()` so both can look the group back up
        # without the caller repeating it.
        self._correlation_group_by_ticker: dict[str, str] = {}
        # Live truth as of the last `reconcile_open_positions()` call: ticker/group -> open
        # contracts right now, straight from `/portfolio/positions`. Empty with
        # `_reconciled_at_ms == 0` before the first reconciliation completes (briefly, at boot).
        # Position checks fall back to a pure 24h `_fills` window in that gap, same as before
        # this method existed.
        self._reconciled_position_by_ticker: dict[str, float] = {}
        self._reconciled_position_by_group: dict[str, float] = {}
        # Same baseline, aggregated by event (`event_ticker_of`). Rebuilt from `open_positions`
        # on every reconciliation rather than tracked incrementally, so it cannot drift.
        self._reconciled_position_by_event: dict[str, float] = {}
        self._reconciled_at_ms = 0
        self._in_flight = 0

    @classmethod
    def warm_start(cls, limits: RiskLimits, db_path: Path, now_ms: int) -> RiskGate:
        """Build a gate whose state reflects the last 24 hours of `orders_fired`.

        The one database read this module ever performs, at startup, before the first wake,
        never at fire time. Rejected rows are excluded throughout: they never reached the wire,
        and counting them would let the gate throttle itself on its own refusals. Position caps
        read from this warm start only until the first `reconcile_open_positions()` call lands
        (typically within seconds, since `account_monitor`'s poll fetches immediately on startup);
        until then, `_fills` alone stands in for current exposure.

        Args:
            limits: The caps to enforce.
            db_path: The telemetry database `orders_fired` lives in.
            now_ms: Current wall-clock epoch milliseconds.

        Returns:
            A gate warm-started from the audit trail.
        """
        gate = cls(limits)
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT ticker, correlation_group, outcome_side, count, price_dollars,"
                " fill_count, requested_at_ms"
                " FROM orders_fired"
                " WHERE requested_at_ms >= ? AND status != 'rejected'"
                " ORDER BY requested_at_ms",
                (now_ms - _DAY_MS,),
            ).fetchall()
        finally:
            conn.close()

        for (
            ticker,
            correlation_group,
            outcome_side,
            count,
            price_dollars,
            fill_count,
            requested_at_ms,
        ) in rows:
            correlation_group = correlation_group or ""
            gate._attempts.append(
                (
                    requested_at_ms,
                    ticker,
                    correlation_group,
                    _as_float(count) * _as_float(price_dollars),
                )
            )
            key = (ticker, outcome_side)
            gate._last_attempt_ms[key] = max(gate._last_attempt_ms.get(key, 0), requested_at_ms)
            if correlation_group:
                gate._correlation_group_by_ticker[ticker] = correlation_group
            if fill_count is not None:
                fill = _as_float(fill_count)
                if fill > 0:
                    gate._fills.append((requested_at_ms, ticker, correlation_group, fill))
        return gate

    def check(
        self,
        ticker: str,
        direction: str,
        exchange_index: int,
        count: int,
        price_dollars: float,
        now_ms: int,
        correlation_group: str = "",
    ) -> str | None:
        """Decide whether a real dispatch may proceed. Pure arithmetic, no I/O.

        Args:
            ticker: Market ticker the order targets.
            direction: Outcome side, `"yes"` or `"no"`.
            exchange_index: Shard the order would be routed to (`-1` = unknown).
            count: Contracts the order would request, after liquidity sizing.
            price_dollars: YES-side wire price the order would carry.
            now_ms: Current wall-clock epoch milliseconds.
            correlation_group: `AssetConfig.correlation_group` for this ticker's asset (e.g. an
                hourly series and its 15-minute counterpart on the same underlying share one).
                `""` (a pre-this-field wake, or an asset with no group) skips the group caps
                below rather than guessing a group to check against.

        Returns:
            `None` to allow, else a short operator-facing rejection reason recorded in the
            `rejected` row's `error_message`.
        """
        limits = self.limits
        if exchange_index not in limits.allowed_exchange_indexes:
            unknown = (
                " (unknown shard cannot be verified as funded)" if exchange_index == -1 else ""
            )
            return (
                f"exchange_index={exchange_index} not in allowed "
                f"{sorted(limits.allowed_exchange_indexes)}{unknown}"
            )
        if self._in_flight >= limits.max_concurrent_dispatches:
            return (
                f"{self._in_flight} dispatches already in flight "
                f"(max {limits.max_concurrent_dispatches})"
            )

        last = self._last_attempt_ms.get((ticker, direction))
        if last is not None and now_ms - last < limits.refire_cooldown_seconds * 1000:
            return (
                f"cooldown: last {direction} attempt on {ticker} was "
                f"{(now_ms - last) / 1000:.1f}s ago (min {limits.refire_cooldown_seconds:.0f}s)"
            )

        self._prune(now_ms)
        hour_floor = now_ms - _HOUR_MS
        attempts_last_hour = sum(1 for ts, _, _, _ in self._attempts if ts >= hour_floor)
        if attempts_last_hour >= limits.max_attempts_per_hour:
            return (
                f"{attempts_last_hour} attempts in the last hour "
                f"(max {limits.max_attempts_per_hour})"
            )
        if len(self._attempts) >= limits.max_attempts_per_day:
            return (
                f"{len(self._attempts)} attempts in the last 24h "
                f"(max {limits.max_attempts_per_day})"
            )

        ticker_attempts = sum(1 for _, t, _, _ in self._attempts if t == ticker)
        if ticker_attempts >= limits.max_attempts_per_ticker_per_day:
            return (
                f"{ticker_attempts} attempts on {ticker} in the last 24h "
                f"(max {limits.max_attempts_per_ticker_per_day})"
            )

        event_ticker = event_ticker_of(ticker)
        event_attempts = sum(
            1 for _, t, _, _ in self._attempts if event_ticker_of(t) == event_ticker
        )
        if event_attempts >= limits.max_attempts_per_event_per_day:
            return (
                f"{event_attempts} attempts across event {event_ticker!r} in the last 24h "
                f"(max {limits.max_attempts_per_event_per_day})"
            )

        if correlation_group:
            group_attempts = sum(1 for _, _, g, _ in self._attempts if g == correlation_group)
            if group_attempts >= limits.max_attempts_per_correlation_group_per_day:
                return (
                    f"{group_attempts} attempts across correlation group {correlation_group!r} "
                    f"in the last 24h (max {limits.max_attempts_per_correlation_group_per_day})"
                )

        notional = sum(n for _, _, _, n in self._attempts) + count * price_dollars
        if notional > limits.max_notional_per_day_dollars:
            return (
                f"daily notional would reach ${notional:.2f} "
                f"(max ${limits.max_notional_per_day_dollars:.2f})"
            )

        position = self._position_for_ticker(ticker, now_ms)
        if position + count > limits.max_position_contracts_per_ticker:
            return (
                f"position on {ticker} would reach {position + count:g} contracts "
                f"(max {limits.max_position_contracts_per_ticker:g})"
            )

        event_position = self._position_for_event(event_ticker, now_ms)
        if event_position + count > limits.max_position_contracts_per_event:
            return (
                f"position across event {event_ticker!r} would reach "
                f"{event_position + count:g} contracts "
                f"(max {limits.max_position_contracts_per_event:g})"
            )

        if correlation_group:
            group_position = self._position_for_group(correlation_group, now_ms)
            if group_position + count > limits.max_position_contracts_per_correlation_group:
                return (
                    f"position across correlation group {correlation_group!r} would reach "
                    f"{group_position + count:g} contracts "
                    f"(max {limits.max_position_contracts_per_correlation_group:g})"
                )
        return None

    def record_attempt(
        self,
        ticker: str,
        direction: str,
        count: int,
        price_dollars: float,
        now_ms: int,
        correlation_group: str = "",
    ) -> None:
        """Record one wire attempt the moment it is committed to, before the request is sent.

        Recorded pre-send instead of post-response so a hung or failed dispatch still counts
        against every cap. The money may have moved regardless of what this process saw.
        """
        self._attempts.append((now_ms, ticker, correlation_group, count * price_dollars))
        self._last_attempt_ms[(ticker, direction)] = now_ms
        if correlation_group:
            self._correlation_group_by_ticker[ticker] = correlation_group

    def record_fill(self, ticker: str, fill_count: float, now_ms: int) -> None:
        """Layer a response's `fill_count` on top of the last reconciled position baseline.

        Timestamped so `_position_for_ticker`/`_position_for_group` can tell whether the next
        `reconcile_open_positions()` call is likely to already reflect it (superseded, excluded
        from the live count) or not (still layered on top). See the module docstring.
        """
        if fill_count <= 0:
            return
        correlation_group = self._correlation_group_by_ticker.get(ticker, "")
        self._fills.append((now_ms, ticker, correlation_group, fill_count))

    def reconcile_open_positions(
        self, open_positions: dict[str, float], snapshot_at_ms: int
    ) -> None:
        """Replace the position ledger's baseline with live truth from `/portfolio/positions`.

        Called by `execution.account_monitor`'s existing poll (no new I/O, since this consumes data
        it already fetched for the dashboard), never from `check()`/`record_*`. A ticker this bot
        fired into that has since fully settled is simply absent from `open_positions`, since
        Kalshi removes it from `/portfolio/positions` the moment it settles, and that absence *is*
        the "it's closed" signal, never an inference from elapsed time.

        Args:
            open_positions: Ticker -> open contracts (`abs(position_fp)`), from the latest
                `/portfolio/positions` poll. Tickers with zero exposure should be omitted, not
                included as 0.
            snapshot_at_ms: Wall-clock time the underlying fetch was issued (before the request,
                not after). A fill recorded at or after this moment may not be reflected in
                `open_positions` yet and stays layered on top instead of being dropped.
        """
        self._reconciled_position_by_ticker = dict(open_positions)
        groups: dict[str, float] = {}
        for ticker, count in open_positions.items():
            group = self._correlation_group_by_ticker.get(ticker, "")
            if group:
                groups[group] = groups.get(group, 0.0) + count
        self._reconciled_position_by_group = groups
        events: dict[str, float] = {}
        for ticker, count in open_positions.items():
            event = event_ticker_of(ticker)
            events[event] = events.get(event, 0.0) + count
        self._reconciled_position_by_event = events
        self._reconciled_at_ms = snapshot_at_ms

    def begin_dispatch(self) -> None:
        """Mark one dispatch in flight."""
        self._in_flight += 1

    def end_dispatch(self) -> None:
        """Mark one dispatch resolved (response, error, or timeout, anything that returned)."""
        self._in_flight = max(0, self._in_flight - 1)

    def _position_for_ticker(self, ticker: str, now_ms: int) -> float:
        window_start = max(self._reconciled_at_ms, now_ms - _DAY_MS)
        baseline = self._reconciled_position_by_ticker.get(ticker, 0.0)
        unconfirmed = sum(f for ts, t, _, f in self._fills if t == ticker and ts >= window_start)
        return baseline + unconfirmed

    def _position_for_event(self, event_ticker: str, now_ms: int) -> float:
        """Open contracts across every strike of one event, live baseline plus unconfirmed fills.

        Derives each fill's event from its ticker instead of storing it, so the `_fills` and
        `_attempts` tuples keep the shape `warm_start()` already rebuilds from `orders_fired`.
        The deques are bounded by the daily attempt cap, so re-deriving is cheaper than a
        migration.
        """
        window_start = max(self._reconciled_at_ms, now_ms - _DAY_MS)
        baseline = self._reconciled_position_by_event.get(event_ticker, 0.0)
        unconfirmed = sum(
            f
            for ts, t, _, f in self._fills
            if event_ticker_of(t) == event_ticker and ts >= window_start
        )
        return baseline + unconfirmed

    def _position_for_group(self, correlation_group: str, now_ms: int) -> float:
        window_start = max(self._reconciled_at_ms, now_ms - _DAY_MS)
        baseline = self._reconciled_position_by_group.get(correlation_group, 0.0)
        unconfirmed = sum(
            f for ts, _, g, f in self._fills if g == correlation_group and ts >= window_start
        )
        return baseline + unconfirmed

    def _prune(self, now_ms: int) -> None:
        floor = now_ms - _DAY_MS
        while self._attempts and self._attempts[0][0] < floor:
            self._attempts.popleft()
        while self._fills and self._fills[0][0] < floor:
            self._fills.popleft()


def _as_float(value: object) -> float:
    """Parse a fixed-point string (or number) defensively; garbage counts as zero."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
