"""Risk-based position sizing.

Position size is derived from SL distance and a risk-per-trade budget, not
from an LLM percent. The LLM's size_fraction is an *upper bound* that the
risk gate may reduce.

Formula:
    risk_budget_usd = balance_usd * risk_per_trade
    size_usd_notional = risk_budget_usd / sl_distance_pct
    size_units = size_usd_notional / price
    capped by: max_single_position * balance_usd * leverage
"""
from __future__ import annotations

from dataclasses import dataclass

from core.config import Settings
from core.types import MarketFrame, Side


@dataclass
class SizingResult:
    size_units: float
    notional_usd: float
    risk_budget_usd: float
    sl_distance_pct: float
    capped_by: str


def _risk_per_trade(settings: Settings, quality_score: float) -> float:
    """Higher quality -> higher fractional risk, capped by risk_settings."""
    base = 0.01                 # 1% of balance at quality ~0.5
    scaled = base * (0.5 + quality_score)   # 0.75%..1.5%
    return max(0.004, min(scaled, 0.02))


def size_position(
    *,
    balance_usd: float,
    frame: MarketFrame,
    side: Side,
    sl_price: float,
    leverage: int,
    llm_size_fraction: float,
    quality_score: float,
    settings: Settings,
) -> SizingResult:
    price = frame.price
    sl_distance_pct = abs(price - sl_price) / price if price else settings.sl.fixed_pct_fallback
    sl_distance_pct = max(sl_distance_pct, settings.sl.min_sl_pct)

    risk_budget = balance_usd * _risk_per_trade(settings, quality_score)
    risk_based_notional = risk_budget / sl_distance_pct

    llm_cap_notional = balance_usd * min(llm_size_fraction, settings.risk.max_single_position) * max(1, leverage)
    hard_cap_notional = balance_usd * settings.risk.max_single_position * max(1, leverage)

    final_notional = min(risk_based_notional, llm_cap_notional, hard_cap_notional)
    capped_by = "risk"
    if final_notional == llm_cap_notional:
        capped_by = "llm_size"
    if final_notional == hard_cap_notional and final_notional <= llm_cap_notional:
        capped_by = "hard_cap"

    final_notional = max(final_notional, 0.0)
    size_units = final_notional / price if price else 0.0

    return SizingResult(
        size_units=size_units,
        notional_usd=final_notional,
        risk_budget_usd=risk_budget,
        sl_distance_pct=sl_distance_pct,
        capped_by=capped_by,
    )
