"""Kelly-fraction position sizing for real fires.

Replaces `KALSHI_FIXED_ORDER_CONTRACT_COUNT` as a *model*. The constant is retained as a floor
under real fires and as the untouched size for shadow fires (the detect-to-fire measurement design),
rather than being
removed outright.

The formula runs entirely on fields already present on `WakeMessage`/`DecisionResult`: `edge`
and `kalshi_price`. For a "yes" fire, `edge = model_probability - yes_ask_dollars` and
`kalshi_price = yes_ask_dollars`; for a "no" fire, `edge = (1 - model_probability) - no_ask_dollars`
and `kalshi_price = no_ask_dollars` (`decision/fair_value.py::ev_gate`). In both cases `edge` is
exactly `p_win - cost` for that direction's own win-probability and cost, so the standard
single-bet Kelly formula, `f* = edge / (1 - kalshi_price)`, is therefore direction-agnostic: no
branching on `direction` is needed here, and no new wire field had to be added to compute it.

`f*` ("full Kelly") is the theoretically bankroll-growth-optimal fraction to stake, but it is
also extremely sensitive to `model_probability` being even slightly overconfident, since a
mismodeled edge that Kelly takes at face value can devastate a bankroll fast. Two independent
guards exist for that, both operator-tunable (`docs/configuration.md`'s "Position sizing"
section) precisely because they are the first things worth adjusting while a strategy is still
being tuned:

- `kelly_fraction` scales `f*` down (e.g. `0.15` = stake 15% of what full Kelly would), the
  standard "fractional Kelly" practice for exactly this reason.
- `max_position_pct_of_balance` is a hard ceiling on the staked fraction regardless of what the
  (scaled) Kelly formula says. Model error compounds fastest on the largest bets, so this caps
  the damage a single badly-mismodeled edge can do in one fire.

Balance is read from `BalanceCache`, a plain in-memory holder `account_monitor.py`'s sidecar poll
refreshes every `DEFAULT_INTERVAL_SECONDS`, never fetched here, per `ENGINEERING.md` rule 1 (state
pre-computed at startup/on an interval, not queried at fire time).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BalanceCache:
    """Mutable holder for the account's spendable balance, refreshed by a sidecar poll.

    A plain dataclass, not `frozen`, because `account_monitor.poll_account_periodically` mutates
    one shared instance in place every interval; `size_position()` reads whatever the most recent
    write left behind. No lock: asyncio is single-threaded and cooperative, so a coroutine's
    plain-attribute write and another coroutine's read can never interleave mid-update. The
    reader always sees either the old value or the fully-written new one, never a partial one.

    Attributes:
        balance_dollars: Sum of `balance_breakdown` entries for the shards this deployment is
            allowed to trade on (`KalshiBotConfig.allowed_exchange_indexes`), and never the
            account's total balance, which may include shards this bot cannot route an order to.
            `0.0` until the first poll completes, which `size_position` reads as "unknown, use the
            floor" instead of "confirmed empty".
        updated_at_ms: Wall-clock time of the write that produced `balance_dollars`, informational.
    """

    balance_dollars: float = 0.0
    updated_at_ms: int = 0


def size_position(
    edge: float,
    kalshi_price: float,
    balance_dollars: float,
    *,
    kelly_fraction: float,
    max_position_pct_of_balance: float,
    min_contract_count: int,
) -> int:
    """Contract count for a real fire: fractional-Kelly stake, capped, floored.

    Pure arithmetic, no I/O, so it is safe to call on the hot path (`ENGINEERING.md` rule 1).

    Args:
        edge: `WakeMessage.edge`, this direction's `p_win - cost`, already positive for any
            fire that reached here (the EV gate requires `edge > fee + margin` to fire at all).
        kalshi_price: `WakeMessage.kalshi_price`, this direction's effective cost, in `(0, 1)`.
        balance_dollars: `BalanceCache.balance_dollars` at the moment of the fire.
        kelly_fraction: Fraction of full Kelly to actually stake (e.g. `0.15`). Clamped to
            `>= 0.0`; a negative config value would flip every stake to buying the wrong amount
            of the right side rather than refusing to size.
        max_position_pct_of_balance: Hard ceiling on the staked fraction of `balance_dollars`,
            applied after `kelly_fraction`, the backstop against a mismodeled edge sizing far
            too aggressively even at a fractional Kelly.
        min_contract_count: Floor on the returned count, applied last. Matches
            `KalshiBotConfig.fixed_order_contract_count`, the same constant that sized every
            real fire before this model existed, now the minimum instead of the only size.

    Returns:
        Contract count to request, before the resting-liquidity cap (`available_size_contracts`)
        the caller applies on top of this. Always `>= min_contract_count`.
    """
    if not 0.0 < kalshi_price < 1.0 or balance_dollars <= 0.0:
        return min_contract_count

    full_kelly_fraction = max(0.0, edge) / (1.0 - kalshi_price)
    stake_fraction = min(
        full_kelly_fraction * max(0.0, kelly_fraction), max_position_pct_of_balance
    )
    stake_dollars = stake_fraction * balance_dollars
    count = int(stake_dollars / kalshi_price)
    return max(count, min_contract_count)
