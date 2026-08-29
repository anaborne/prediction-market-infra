"""Tests for `build_template`."""

from __future__ import annotations

import pytest

from kalshi_bot.execution.prebuilt_orders import PrebuiltOrderTemplate, build_template


def test_build_template_returns_populated_template() -> None:
    template = build_template("KXTEST", "yes")

    assert template == PrebuiltOrderTemplate(ticker="KXTEST", outcome_side="yes")


def test_build_template_rejects_invalid_outcome_side() -> None:
    with pytest.raises(ValueError, match="outcome_side"):
        build_template("KXTEST", "maybe")
