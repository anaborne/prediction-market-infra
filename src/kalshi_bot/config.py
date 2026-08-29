"""Configuration loading for kalshi_bot.

Defines the typed configuration structure the rest of the bot depends on, and a loader that
reads values from environment variables (see `.env.example` for the expected variable names).
No validation of values (e.g. checking that a private key file exists, or that a URL is
well-formed) is performed here yet, and it will be added alongside the modules that need it.
The exceptions are `environment`, `order_time_in_force`, `order_self_trade_prevention_type` and
`allowed_exchange_indexes`: each of these picks real-order-execution behavior (which API host, how
a live fire executes, and which shards it may route to), so each gets a runtime check even though
the rest of this loader performs none.

`OrderTimeInForce`/`OrderSelfTradePreventionType` are defined locally instead of imported from
`execution.order_dispatcher` (whose `TimeInForce`/`SelfTradePreventionType` share the exact same
values) to keep `config` a leaf module with no dependency on `execution`. See
`docs/GUIDE.md §7`'s module boundaries.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_ENVIRONMENTS = ("demo", "prod")

OrderTimeInForce = Literal["fill_or_kill", "good_till_canceled", "immediate_or_cancel"]
OrderSelfTradePreventionType = Literal["taker_at_cross", "maker"]

_ORDER_TIME_IN_FORCE_VALUES: tuple[OrderTimeInForce, ...] = (
    "fill_or_kill",
    "good_till_canceled",
    "immediate_or_cancel",
)
_ORDER_SELF_TRADE_PREVENTION_TYPE_VALUES: tuple[OrderSelfTradePreventionType, ...] = (
    "taker_at_cross",
    "maker",
)


@dataclass(frozen=True, slots=True)
class KalshiBotConfig:
    """Runtime configuration for kalshi_bot.

    Attributes:
        api_key_id: Kalshi API key identifier used for request authentication.
        private_key_path: Filesystem path to the PEM-encoded RSA private key used for signing.
        demo_base_url: REST base URL for the Kalshi demo (paper trading) environment.
        prod_base_url: REST base URL for the Kalshi production environment.
        demo_ws_url: WebSocket base URL for the Kalshi demo environment. Distinct host from
            `demo_base_url`. See `ENGINEERING.md`'s "Kalshi API v2 auth spec".
        prod_ws_url: WebSocket base URL for the Kalshi production environment.
        environment: Which environment to target, either "demo" or "prod".
        telemetry_db_path: Filesystem path to the SQLite database used for telemetry.
        ipc_socket_path: Filesystem path of the Unix domain socket the executor process listens
            on and the poller process connects to (Phase 8). See `docs/GUIDE.md`.
        fixed_order_contract_count: Number of contracts the executor places on every real fire.
            A fixed placeholder and no sizing model. See the poller/executor IPC design.
        order_time_in_force: `time_in_force` the executor uses for every real fire.
        order_self_trade_prevention_type: `self_trade_prevention_type` the executor uses for
            every real fire.
        allowed_exchange_indexes: Demo shards (`exchange_index` values) this deployment is
            allowed to dispatch real orders to. The executor refuses to start when this is empty
            or names a shard the account holds no balance on (`funded_exchange_indexes()`), per
            the shard gate. A constraint that cost three phases of misdiagnosis should
            fail loudly at startup, not silently at fire time. Empty means "not configured",
            never "no restriction".
        killswitch_path: Filesystem path whose existence halts new order dispatch
            (`runtime/killswitch.py`). Checked by the executor before every real fire; operated
            from a shell via `scripts/killswitch.py`. Both must resolve it to the same path.
        log_dir: Directory the entry-point scripts write their rotating log files into
            (`runtime/logging_setup.py`), one file per process.
        risk_gate_enabled: Whether `scripts/run_executor.py` constructs a `RiskGate` at all.
            Defaults to `True`. It defaulted to `False` through the demo-trading phase, on the
            argument that a bot meant to fire on every edge it finds should not stop at a fixed
            budget, but that made the *unset* value the *unsafe* one, which contradicts the rule
            the individual limits below already follow ("unset means safe, not unlimited"), and it
            meant an operator who set every `KALSHI_RISK_*` cap carefully still got no gate at all
            unless they also knew about this switch. `KALSHI_RISK_GATE_ENABLED=false` still turns
            it off explicitly, which is the right shape: disabling every cap should take a
            deliberate act, not an omission. `False` passes `risk_gate=None` to `ExecutorServer`,
            which documents `None` as disabling the check entirely. See `docs/configuration.md`'s
            "Risk gate" section.
        risk_max_attempts_per_hour: `execution.risk.RiskLimits.max_attempts_per_hour`.
        risk_max_attempts_per_day: `execution.risk.RiskLimits.max_attempts_per_day`.
        risk_max_attempts_per_ticker_per_day:
            `execution.risk.RiskLimits.max_attempts_per_ticker_per_day`.
        risk_max_notional_per_day_dollars: `execution.risk.RiskLimits.max_notional_per_day_dollars`.
        risk_max_concurrent_dispatches: `execution.risk.RiskLimits.max_concurrent_dispatches`.
        risk_max_position_contracts_per_ticker:
            `execution.risk.RiskLimits.max_position_contracts_per_ticker`.
        risk_max_attempts_per_correlation_group_per_day:
            `execution.risk.RiskLimits.max_attempts_per_correlation_group_per_day`.
        risk_max_position_contracts_per_correlation_group:
            `execution.risk.RiskLimits.max_position_contracts_per_correlation_group`.
        risk_max_attempts_per_event_per_day:
            `execution.risk.RiskLimits.max_attempts_per_event_per_day`.
        risk_max_position_contracts_per_event:
            `execution.risk.RiskLimits.max_position_contracts_per_event`. These two were enforced
            by `RiskGate` but had no configuration surface and were not passed by
            `scripts/run_executor.py`, so they silently took `RiskLimits`' dataclass defaults and
            no operator could tune them. `tests/test_config.py` now pins that every
            `RiskLimits` field except `allowed_exchange_indexes` is reachable from here, so the
            next cap added to `RiskLimits` cannot go unwired the same way.
        risk_refire_cooldown_seconds: `execution.risk.RiskLimits.refire_cooldown_seconds`.
            Defaults match `RiskLimits`' own conservative defaults, so leaving all eleven unset
            reproduces `RiskLimits`' code defaults exactly. See `docs/configuration.md`'s "Risk
            gate" section for what each one caps and how to tighten them back down before ever
            pointing at real money.
        position_sizing_kelly_fraction: `ipc.executor_server.ExecutorServer.kelly_fraction`, the
            fraction of full Kelly staked on a real fire. See `docs/configuration.md`'s
            "Position sizing" section.
        position_sizing_max_pct_of_balance: `ipc.executor_server.ExecutorServer`'s
            `max_position_pct_of_balance`, the hard ceiling on the staked fraction of spendable
            balance, applied after `position_sizing_kelly_fraction`.
    """

    api_key_id: str
    private_key_path: Path
    demo_api_key_id: str
    demo_private_key_path: Path
    prod_api_key_id: str
    prod_private_key_path: Path
    demo_base_url: str
    prod_base_url: str
    demo_ws_url: str
    prod_ws_url: str
    environment: Literal["demo", "prod"]
    allow_production_orders: bool
    telemetry_db_path: Path
    ipc_socket_path: Path
    fixed_order_contract_count: int
    order_time_in_force: OrderTimeInForce
    order_self_trade_prevention_type: OrderSelfTradePreventionType
    allowed_exchange_indexes: frozenset[int]
    killswitch_path: Path
    log_dir: Path
    risk_gate_enabled: bool
    risk_max_attempts_per_hour: int
    risk_max_attempts_per_day: int
    risk_max_attempts_per_ticker_per_day: int
    risk_max_notional_per_day_dollars: float
    risk_max_concurrent_dispatches: int
    risk_max_position_contracts_per_ticker: float
    risk_max_attempts_per_correlation_group_per_day: int
    risk_max_position_contracts_per_correlation_group: float
    risk_max_attempts_per_event_per_day: int
    risk_max_position_contracts_per_event: float
    risk_refire_cooldown_seconds: float
    position_sizing_kelly_fraction: float
    position_sizing_max_pct_of_balance: float

    @property
    def base_url(self) -> str:
        """REST base URL for the selected environment.

        Returns:
            `prod_base_url` when `environment == "prod"`, else `demo_base_url`. Call this instead
            of picking a URL at each call site, because hard-coding one is how a process ends up
            talking to an exchange nobody chose.
        """
        return self.prod_base_url if self.environment == "prod" else self.demo_base_url

    @property
    def ws_url(self) -> str:
        """WebSocket base URL for the selected environment.

        Returns:
            `prod_ws_url` when `environment == "prod"`, else `demo_ws_url`.
        """
        return self.prod_ws_url if self.environment == "prod" else self.demo_ws_url

    @property
    def resolved_api_key_id(self) -> str:
        """API key id for the selected environment.

        Kalshi API keys are environment-specific. A production key returns `401
        authentication_error` against demo and vice versa, verified both directions (`docs/GUIDE.md`
        §2.10). So each environment needs its own credential, and the environment-specific variable
        wins when set.

        Returns:
            `KALSHI_{ENV}_API_KEY_ID` when set, otherwise the shared `KALSHI_API_KEY_ID`.
        """
        specific = self.prod_api_key_id if self.environment == "prod" else self.demo_api_key_id
        return specific or self.api_key_id

    @property
    def resolved_private_key_path(self) -> Path:
        """Private key path for the selected environment.

        Returns:
            `KALSHI_{ENV}_PRIVATE_KEY_PATH` when set, otherwise the shared
            `KALSHI_PRIVATE_KEY_PATH`.
        """
        specific = (
            self.prod_private_key_path if self.environment == "prod" else self.demo_private_key_path
        )
        return specific if str(specific) not in ("", ".") else self.private_key_path

    def assert_orders_permitted(self) -> None:
        """Refuse to place orders against production unless that was chosen deliberately.

        Reading production market data is ordinary and unrestricted, and it is what the whole
        research layer does. Placing an order against production is a different decision, and
        this is the greppable chokepoint any order path must pass, the analogue of
        `crossvenue.venues.assert_us_executable`. Selecting the production *environment* is not
        by itself consent to trade real money on it: that takes a second, explicit switch, so
        that no single edit or typo can turn a data session into a live trade.

        Raises:
            RuntimeError: If the environment is production and `KALSHI_ALLOW_PRODUCTION_ORDERS`
                is not set true.
        """
        if self.environment == "prod" and not self.allow_production_orders:
            raise RuntimeError(
                "Refusing to place orders against PRODUCTION: KALSHI_ENVIRONMENT=prod but "
                "KALSHI_ALLOW_PRODUCTION_ORDERS is not true. Reading production data needs no "
                "flag; trading real money needs this one set deliberately."
            )

    def assert_demo_trading_process(self) -> None:
        """Refuse to start the poller or the executor anywhere but demo.

        This is the second, stronger of the two environment guards, and it is deliberately
        not the same rule as `assert_orders_permitted`. That one asks "may an order be placed
        against production?", and answers yes once `KALSHI_ALLOW_PRODUCTION_ORDERS` is set. This
        one asks "may *these two long-running processes* run against production?", and the answer
        is no, unconditionally, and no flag lifts it.

        The reason is specific to what those two processes are. They run the directional crypto
        strategy that `docs/GUIDE.md` §4.1 records as falsified out-of-sample, they are
        currently stopped with the kill switch engaged, and nothing about arming a *future* order
        path should silently arm *them*. A production order path, when one exists, will be a new
        entry point that passes `assert_orders_permitted`, and never these.

        `docs/GUIDE.md` §7.3 and `ENGINEERING.md` have both listed this refusal as a structural
        guarantee throughout; it is restored here as an actual mechanism, because a guarantee
        stated only in prose is the failure mode §6 exists to record.

        Raises:
            RuntimeError: If `KALSHI_ENVIRONMENT` is not `demo`.
        """
        if self.environment != "demo":
            raise RuntimeError(
                f"Refusing to start: this process is demo-only, but KALSHI_ENVIRONMENT="
                f"{self.environment!r}. The poller and executor run the falsified strategy "
                "(docs/GUIDE.md §4.1) and must never be pointed at production. "
                "KALSHI_ALLOW_PRODUCTION_ORDERS does not lift this. A production order path "
                "will be a separate entry point, not this one."
            )


def load_config(env: os._Environ[str] | dict[str, str] | None = None) -> KalshiBotConfig:
    """Load a `KalshiBotConfig` from environment variables.

    Args:
        env: Mapping to read variables from. Defaults to `os.environ`.

    Returns:
        A populated `KalshiBotConfig`.

    Raises:
        ValueError: If `KALSHI_ENVIRONMENT` is set to anything other than "demo" or "prod",
            the field where an unchecked value could route real orders to the wrong host, or if
            `KALSHI_ORDER_TIME_IN_FORCE`/`KALSHI_ORDER_SELF_TRADE_PREVENTION_TYPE` is set to
            anything outside the values Kalshi's order-creation endpoint accepts, or if
            `KALSHI_ALLOWED_EXCHANGE_INDEXES` contains anything that is not an integer. Each of
            these gets a runtime check even though the rest of this loader performs none, since
            each picks real-order-execution behavior instead of passive configuration. A
            silently-misparsed shard allowlist would either block every dispatch or fail to
            block the wrong ones.
    """
    source = env if env is not None else os.environ

    environment = source.get("KALSHI_ENVIRONMENT", "demo")
    if environment not in _ENVIRONMENTS:
        raise ValueError(f"KALSHI_ENVIRONMENT must be one of {_ENVIRONMENTS}, got {environment!r}")

    order_time_in_force = source.get("KALSHI_ORDER_TIME_IN_FORCE", "fill_or_kill")
    if order_time_in_force not in _ORDER_TIME_IN_FORCE_VALUES:
        raise ValueError(
            "KALSHI_ORDER_TIME_IN_FORCE must be one of "
            f"{_ORDER_TIME_IN_FORCE_VALUES}, got {order_time_in_force!r}"
        )

    order_self_trade_prevention_type = source.get(
        "KALSHI_ORDER_SELF_TRADE_PREVENTION_TYPE", "taker_at_cross"
    )
    if order_self_trade_prevention_type not in _ORDER_SELF_TRADE_PREVENTION_TYPE_VALUES:
        raise ValueError(
            "KALSHI_ORDER_SELF_TRADE_PREVENTION_TYPE must be one of "
            f"{_ORDER_SELF_TRADE_PREVENTION_TYPE_VALUES}, got {order_self_trade_prevention_type!r}"
        )

    raw_allowed = source.get("KALSHI_ALLOWED_EXCHANGE_INDEXES", "")
    try:
        allowed_exchange_indexes = frozenset(
            int(part.strip()) for part in raw_allowed.split(",") if part.strip()
        )
    except ValueError as exc:
        raise ValueError(
            "KALSHI_ALLOWED_EXCHANGE_INDEXES must be a comma-separated list of integers "
            f"(e.g. '2' or '0,2'), got {raw_allowed!r}"
        ) from exc

    return KalshiBotConfig(
        api_key_id=source.get("KALSHI_API_KEY_ID", ""),
        private_key_path=Path(source.get("KALSHI_PRIVATE_KEY_PATH", "")),
        demo_api_key_id=source.get("KALSHI_DEMO_API_KEY_ID", ""),
        demo_private_key_path=Path(source.get("KALSHI_DEMO_PRIVATE_KEY_PATH", "")),
        prod_api_key_id=source.get("KALSHI_PROD_API_KEY_ID", ""),
        prod_private_key_path=Path(source.get("KALSHI_PROD_PRIVATE_KEY_PATH", "")),
        demo_base_url=source.get("KALSHI_DEMO_BASE_URL", ""),
        prod_base_url=source.get("KALSHI_PROD_BASE_URL", ""),
        demo_ws_url=source.get("KALSHI_DEMO_WS_URL", ""),
        prod_ws_url=source.get("KALSHI_PROD_WS_URL", ""),
        environment=environment,  # type: ignore[arg-type]  # narrowed by the membership check above
        allow_production_orders=source.get("KALSHI_ALLOW_PRODUCTION_ORDERS", "").strip().lower()
        in ("1", "true", "yes"),
        telemetry_db_path=Path(source.get("KALSHI_TELEMETRY_DB_PATH", "")),
        ipc_socket_path=Path(source.get("KALSHI_IPC_SOCKET_PATH", "./data/executor.sock")),
        fixed_order_contract_count=int(source.get("KALSHI_FIXED_ORDER_CONTRACT_COUNT", "1")),
        order_time_in_force=order_time_in_force,
        order_self_trade_prevention_type=order_self_trade_prevention_type,
        allowed_exchange_indexes=allowed_exchange_indexes,
        killswitch_path=Path(source.get("KALSHI_KILLSWITCH_PATH", "./data/killswitch")),
        log_dir=Path(source.get("KALSHI_LOG_DIR", "./data/logs")),
        # On by default. It was off through the demo-trading phase, on the argument that
        # count/notional/position caps constrain a strategy meant to fire on every edge it finds.
        # That made the *unset* value the *unsafe* one, the opposite of the rule the individual
        # limits just below follow, and a trap for an operator who tunes every KALSHI_RISK_* cap
        # and still gets no gate. Turning every cap off now takes writing "false" on purpose.
        risk_gate_enabled=source.get("KALSHI_RISK_GATE_ENABLED", "true").strip().lower()
        in ("1", "true", "yes"),
        # Defaults reproduce execution.risk.RiskLimits' own field defaults exactly, so an
        # operator who sets none of these gets the same conservative caps as before this
        # config surface existed: unset means safe, not unlimited.
        risk_max_attempts_per_hour=int(source.get("KALSHI_RISK_MAX_ATTEMPTS_PER_HOUR", "10")),
        risk_max_attempts_per_day=int(source.get("KALSHI_RISK_MAX_ATTEMPTS_PER_DAY", "50")),
        risk_max_attempts_per_ticker_per_day=int(
            source.get("KALSHI_RISK_MAX_ATTEMPTS_PER_TICKER_PER_DAY", "5")
        ),
        risk_max_notional_per_day_dollars=float(
            source.get("KALSHI_RISK_MAX_NOTIONAL_PER_DAY_DOLLARS", "50.0")
        ),
        risk_max_concurrent_dispatches=int(
            source.get("KALSHI_RISK_MAX_CONCURRENT_DISPATCHES", "2")
        ),
        risk_max_position_contracts_per_ticker=float(
            source.get("KALSHI_RISK_MAX_POSITION_CONTRACTS_PER_TICKER", "5.0")
        ),
        risk_max_attempts_per_correlation_group_per_day=int(
            source.get("KALSHI_RISK_MAX_ATTEMPTS_PER_CORRELATION_GROUP_PER_DAY", "10")
        ),
        risk_max_attempts_per_event_per_day=int(
            source.get("KALSHI_RISK_MAX_ATTEMPTS_PER_EVENT_PER_DAY", "6")
        ),
        risk_max_position_contracts_per_event=float(
            source.get("KALSHI_RISK_MAX_POSITION_CONTRACTS_PER_EVENT", "6.0")
        ),
        risk_max_position_contracts_per_correlation_group=float(
            source.get("KALSHI_RISK_MAX_POSITION_CONTRACTS_PER_CORRELATION_GROUP", "10.0")
        ),
        risk_refire_cooldown_seconds=float(
            source.get("KALSHI_RISK_REFIRE_COOLDOWN_SECONDS", "60.0")
        ),
        # Defaults reproduce ipc.executor_server's own DEFAULT_KELLY_FRACTION /
        # DEFAULT_MAX_POSITION_PCT_OF_BALANCE exactly, for the same "unset means the
        # conservative starting point" reason as the risk-gate defaults above.
        position_sizing_kelly_fraction=float(source.get("KALSHI_KELLY_FRACTION", "0.15")),
        position_sizing_max_pct_of_balance=float(
            source.get("KALSHI_MAX_POSITION_PCT_OF_BALANCE", "0.02")
        ),
    )
