"""Tests for `load_config`."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from kalshi_bot.config import load_config
from kalshi_bot.execution.risk import RiskLimits

_FULL_ENV = {
    "KALSHI_API_KEY_ID": "test-key-id",
    "KALSHI_PRIVATE_KEY_PATH": "./secrets/kalshi_demo_private_key.pem",
    "KALSHI_DEMO_BASE_URL": "https://external-api.demo.kalshi.co",
    "KALSHI_PROD_BASE_URL": "https://external-api.kalshi.com",
    "KALSHI_DEMO_WS_URL": "wss://external-api-ws.demo.kalshi.co",
    "KALSHI_PROD_WS_URL": "wss://external-api-ws.kalshi.com",
    "KALSHI_ENVIRONMENT": "demo",
    "KALSHI_TELEMETRY_DB_PATH": "./data/telemetry.sqlite",
    "KALSHI_IPC_SOCKET_PATH": "./data/executor.sock",
    "KALSHI_FIXED_ORDER_CONTRACT_COUNT": "1",
    "KALSHI_ORDER_TIME_IN_FORCE": "fill_or_kill",
    "KALSHI_ORDER_SELF_TRADE_PREVENTION_TYPE": "taker_at_cross",
    "KALSHI_ALLOWED_EXCHANGE_INDEXES": "2",
}


def test_load_config_populates_all_fields_from_env() -> None:
    config = load_config(_FULL_ENV)

    assert config.api_key_id == "test-key-id"
    assert config.private_key_path == Path("./secrets/kalshi_demo_private_key.pem")
    assert config.demo_base_url == "https://external-api.demo.kalshi.co"
    assert config.prod_base_url == "https://external-api.kalshi.com"
    assert config.demo_ws_url == "wss://external-api-ws.demo.kalshi.co"
    assert config.prod_ws_url == "wss://external-api-ws.kalshi.com"
    assert config.environment == "demo"
    assert config.telemetry_db_path == Path("./data/telemetry.sqlite")
    assert config.ipc_socket_path == Path("./data/executor.sock")
    assert config.fixed_order_contract_count == 1
    assert config.order_time_in_force == "fill_or_kill"
    assert config.order_self_trade_prevention_type == "taker_at_cross"
    assert config.allowed_exchange_indexes == frozenset({2})
    assert config.killswitch_path == Path("./data/killswitch")
    assert config.log_dir == Path("./data/logs")
    # Unset in _FULL_ENV, so this is the default: on. See the risk-gate section below.
    assert config.risk_gate_enabled is True
    assert config.risk_max_attempts_per_hour == 10
    assert config.risk_max_attempts_per_day == 50
    assert config.risk_max_attempts_per_ticker_per_day == 5
    assert config.risk_max_notional_per_day_dollars == 50.0
    assert config.risk_max_concurrent_dispatches == 2
    assert config.risk_max_position_contracts_per_ticker == 5.0
    assert config.risk_max_attempts_per_correlation_group_per_day == 10
    assert config.risk_max_position_contracts_per_correlation_group == 10.0
    assert config.risk_refire_cooldown_seconds == 60.0
    assert config.position_sizing_kelly_fraction == 0.15
    assert config.position_sizing_max_pct_of_balance == 0.02


def test_load_config_accepts_killswitch_and_log_overrides() -> None:
    env = {
        **_FULL_ENV,
        "KALSHI_KILLSWITCH_PATH": "/var/run/kalshi/halt",
        "KALSHI_LOG_DIR": "/var/log/kalshi",
    }

    config = load_config(env)

    assert config.killswitch_path == Path("/var/run/kalshi/halt")
    assert config.log_dir == Path("/var/log/kalshi")


def test_load_config_parses_a_multi_shard_allowlist_with_spaces() -> None:
    env = {**_FULL_ENV, "KALSHI_ALLOWED_EXCHANGE_INDEXES": " 0, 2 "}

    config = load_config(env)

    assert config.allowed_exchange_indexes == frozenset({0, 2})


def test_load_config_defaults_the_shard_allowlist_to_empty() -> None:
    """Empty means "not configured", so the executor refuses to start on it, never "anything"."""
    env = {k: v for k, v in _FULL_ENV.items() if k != "KALSHI_ALLOWED_EXCHANGE_INDEXES"}

    config = load_config(env)

    assert config.allowed_exchange_indexes == frozenset()


def test_load_config_rejects_a_non_integer_shard_allowlist() -> None:
    env = {**_FULL_ENV, "KALSHI_ALLOWED_EXCHANGE_INDEXES": "2,crypto"}

    with pytest.raises(ValueError, match="KALSHI_ALLOWED_EXCHANGE_INDEXES"):
        load_config(env)


@pytest.mark.parametrize("value", ["true", "True", "1", "yes", "YES"])
def test_load_config_accepts_truthy_risk_gate_enabled_values(value: str) -> None:
    env = {**_FULL_ENV, "KALSHI_RISK_GATE_ENABLED": value}

    config = load_config(env)

    assert config.risk_gate_enabled is True


@pytest.mark.parametrize("value", ["false", "False", "0", "no", ""])
def test_load_config_accepts_falsy_risk_gate_enabled_values(value: str) -> None:
    env = {**_FULL_ENV, "KALSHI_RISK_GATE_ENABLED": value}

    config = load_config(env)

    assert config.risk_gate_enabled is False


def test_load_config_accepts_risk_limit_overrides() -> None:
    """Every field but the shard allowlist is env-configurable, per the paper-trading
    calibration done on 2026-08-21. See docs/configuration.md's "Risk gate" section."""
    env = {
        **_FULL_ENV,
        "KALSHI_RISK_MAX_ATTEMPTS_PER_HOUR": "200",
        "KALSHI_RISK_MAX_ATTEMPTS_PER_DAY": "2000",
        "KALSHI_RISK_MAX_ATTEMPTS_PER_TICKER_PER_DAY": "200",
        "KALSHI_RISK_MAX_NOTIONAL_PER_DAY_DOLLARS": "2000",
        "KALSHI_RISK_MAX_CONCURRENT_DISPATCHES": "8",
        "KALSHI_RISK_MAX_POSITION_CONTRACTS_PER_TICKER": "100",
        "KALSHI_RISK_MAX_ATTEMPTS_PER_CORRELATION_GROUP_PER_DAY": "400",
        "KALSHI_RISK_MAX_POSITION_CONTRACTS_PER_CORRELATION_GROUP": "200",
        "KALSHI_RISK_MAX_ATTEMPTS_PER_EVENT_PER_DAY": "300",
        "KALSHI_RISK_MAX_POSITION_CONTRACTS_PER_EVENT": "150",
        "KALSHI_RISK_REFIRE_COOLDOWN_SECONDS": "15",
    }

    config = load_config(env)

    assert config.risk_max_attempts_per_hour == 200
    assert config.risk_max_attempts_per_day == 2000
    assert config.risk_max_attempts_per_ticker_per_day == 200
    assert config.risk_max_notional_per_day_dollars == 2000.0
    assert config.risk_max_concurrent_dispatches == 8
    assert config.risk_max_position_contracts_per_ticker == 100.0
    assert config.risk_max_attempts_per_correlation_group_per_day == 400
    assert config.risk_max_position_contracts_per_correlation_group == 200.0
    assert config.risk_max_attempts_per_event_per_day == 300
    assert config.risk_max_position_contracts_per_event == 150.0
    assert config.risk_refire_cooldown_seconds == 15.0


def test_load_config_accepts_position_sizing_overrides() -> None:
    env = {
        **_FULL_ENV,
        "KALSHI_KELLY_FRACTION": "0.5",
        "KALSHI_MAX_POSITION_PCT_OF_BALANCE": "0.05",
    }

    config = load_config(env)

    assert config.position_sizing_kelly_fraction == 0.5
    assert config.position_sizing_max_pct_of_balance == 0.05


def test_load_config_defaults_new_phase_8_fields_when_unset() -> None:
    env = {
        k: v
        for k, v in _FULL_ENV.items()
        if k
        not in (
            "KALSHI_IPC_SOCKET_PATH",
            "KALSHI_FIXED_ORDER_CONTRACT_COUNT",
            "KALSHI_ORDER_TIME_IN_FORCE",
            "KALSHI_ORDER_SELF_TRADE_PREVENTION_TYPE",
        )
    }

    config = load_config(env)

    assert config.ipc_socket_path == Path("./data/executor.sock")
    assert config.fixed_order_contract_count == 1
    assert config.order_time_in_force == "fill_or_kill"
    assert config.order_self_trade_prevention_type == "taker_at_cross"


def test_load_config_accepts_overridden_order_execution_values() -> None:
    env = {
        **_FULL_ENV,
        "KALSHI_ORDER_TIME_IN_FORCE": "immediate_or_cancel",
        "KALSHI_ORDER_SELF_TRADE_PREVENTION_TYPE": "maker",
        "KALSHI_FIXED_ORDER_CONTRACT_COUNT": "5",
    }

    config = load_config(env)

    assert config.order_time_in_force == "immediate_or_cancel"
    assert config.order_self_trade_prevention_type == "maker"
    assert config.fixed_order_contract_count == 5


def test_load_config_rejects_an_invalid_order_time_in_force_value() -> None:
    env = {**_FULL_ENV, "KALSHI_ORDER_TIME_IN_FORCE": "whenever"}

    with pytest.raises(ValueError, match="whenever"):
        load_config(env)


def test_load_config_rejects_an_invalid_self_trade_prevention_type_value() -> None:
    env = {**_FULL_ENV, "KALSHI_ORDER_SELF_TRADE_PREVENTION_TYPE": "yolo"}

    with pytest.raises(ValueError, match="yolo"):
        load_config(env)


def test_load_config_defaults_environment_to_demo_when_unset() -> None:
    env = {k: v for k, v in _FULL_ENV.items() if k != "KALSHI_ENVIRONMENT"}

    config = load_config(env)

    assert config.environment == "demo"


def test_load_config_accepts_prod_environment() -> None:
    env = {**_FULL_ENV, "KALSHI_ENVIRONMENT": "prod"}

    config = load_config(env)

    assert config.environment == "prod"


def test_load_config_rejects_an_invalid_environment_value() -> None:
    env = {**_FULL_ENV, "KALSHI_ENVIRONMENT": "staging"}

    with pytest.raises(ValueError, match="staging"):
        load_config(env)


def test_the_risk_gate_is_on_when_nothing_says_otherwise() -> None:
    """Unset must mean *safe*, not *unlimited*.

    This defaulted to `False` through the demo-trading phase, which made the omission of a
    variable the thing that removed every cap, and an operator could set all eleven
    `KALSHI_RISK_*` limits carefully and still get no gate at all.
    """
    env = {k: v for k, v in _FULL_ENV.items() if k != "KALSHI_RISK_GATE_ENABLED"}

    assert load_config(env).risk_gate_enabled is True


def test_the_risk_gate_can_still_be_turned_off_deliberately() -> None:
    assert (
        load_config({**_FULL_ENV, "KALSHI_RISK_GATE_ENABLED": "false"}).risk_gate_enabled is False
    )


def test_every_risk_limit_is_reachable_from_configuration() -> None:
    """No cap may be enforceable but untunable.

    `RiskGate` enforced `max_attempts_per_event_per_day` and `max_position_contracts_per_event`
    while `load_config` had no field for either and `scripts/run_executor.py` never passed them,
    so both silently took `RiskLimits`' dataclass defaults and no operator could move them. This
    test is the mechanism that stops the next cap added to `RiskLimits` from repeating it: it
    reads the dataclass's own field list rather than a hand-maintained copy.

    `allowed_exchange_indexes` is exempt by design. It has no default precisely so the caller
    must wire it from config, and it already is (`KALSHI_ALLOWED_EXCHANGE_INDEXES`).
    """
    config = load_config(_FULL_ENV)
    exempt = {"allowed_exchange_indexes"}
    missing = [
        field.name
        for field in dataclasses.fields(RiskLimits)
        if field.name not in exempt and not hasattr(config, f"risk_{field.name}")
    ]

    assert missing == [], (
        f"RiskLimits fields with no KalshiBotConfig counterpart: {missing}. Add a "
        f"`risk_{missing[0] if missing else '<field>'}` field and a KALSHI_RISK_* variable, and "
        "pass it in scripts/run_executor.py; an enforced-but-untunable cap is a trap."
    )


def test_the_risk_limit_defaults_match_the_dataclass_defaults() -> None:
    """Leaving every `KALSHI_RISK_*` variable unset must reproduce `RiskLimits`' own defaults.

    Two homes for one number is exactly the drift `ENGINEERING.md`'s "one number, one home" rule
    forbids; this pins them together so a change to either side fails here.
    """
    env = {k: v for k, v in _FULL_ENV.items() if not k.startswith("KALSHI_RISK_MAX")}
    config = load_config(env)
    defaults = RiskLimits(allowed_exchange_indexes=frozenset())

    for field in dataclasses.fields(RiskLimits):
        if field.name == "allowed_exchange_indexes":
            continue
        assert getattr(config, f"risk_{field.name}") == getattr(defaults, field.name), field.name
