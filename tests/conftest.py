"""Shared test fixtures.

None of these tests require a running database or exchange. The orchestrator
is driven with injected fakes; SL/TP/decision math is pure.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Dict, List

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.config import Settings
from core.types import (
    ExternalContext,
    MarketFrame,
    PivotPoints,
    PositionSnapshot,
    Regime,
    RiskState,
    Side,
)


@pytest.fixture
def settings() -> Settings:
    return Settings(tickers=["BTC", "ETH", "SOL"], testnet=True, dry_run=True)


def make_frame(
    ticker: str = "BTC",
    price: float = 100.0,
    atr_pct: float = 0.01,
    regime: Regime = Regime.TRENDING_UP,
    bias: Side | None = Side.LONG,
    rsi14: float = 55.0,
    macd_slope: float = 0.5,
    ema_diff_pct: float | None = None,
) -> MarketFrame:
    if ema_diff_pct is None:
        ema_diff_pct = 0.007 if regime == Regime.TRENDING_UP else \
                       -0.007 if regime == Regime.TRENDING_DOWN else 0.0
    return MarketFrame(
        ticker=ticker,
        ts=datetime.now(timezone.utc),
        price=price,
        ema20=price * (1.0 + ema_diff_pct),
        ema50=price,
        atr14=price * atr_pct,
        atr_pct=atr_pct,
        rsi14=rsi14,
        rsi7=rsi14,
        macd=0.5 if macd_slope > 0 else -0.5,
        macd_slope=macd_slope,
        funding_rate=0.00001,
        open_interest=1000.0,
        pivots=PivotPoints(
            pp=price, s1=price * 0.98, s2=price * 0.96,
            r1=price * 1.02, r2=price * 1.04,
        ),
        volume_ratio=1.1,
        regime=regime,
        structure_bias=bias,
        swing_high=price * 1.03,
        swing_low=price * 0.97,
        recent_closes=[price * (1 + 0.001 * i) for i in range(-10, 0)],
        est_fee_cost=price * 0.00035,
        spread_pct=0.0002,
    )


def make_position(
    symbol: str = "BTC",
    side: Side = Side.LONG,
    entry: float = 100.0,
    mark: float = 101.0,
    sl: float | None = 98.0,
    tp: float | None = 104.0,
    size: float = 0.1,
    trailing: bool = False,
    bars_held: int = 1,
) -> PositionSnapshot:
    pnl_usd = (mark - entry) * size if side == Side.LONG else (entry - mark) * size
    pnl_pct = pnl_usd / (entry * size)
    return PositionSnapshot(
        symbol=symbol,
        side=side,
        size=size,
        entry_price=entry,
        mark_price=mark,
        leverage=2.0,
        pnl_usd=pnl_usd,
        pnl_pct=pnl_pct,
        mae_pct=min(0.0, pnl_pct),
        mfe_pct=max(0.0, pnl_pct),
        bars_held=bars_held,
        opened_at=datetime.now(timezone.utc),
        sl_price=sl,
        tp_price=tp,
        trailing_active=trailing,
        client_order_id=f"pos-{symbol}-test",
    )


@pytest.fixture
def frames_all() -> Dict[str, MarketFrame]:
    return {
        "BTC": make_frame("BTC", 95000.0),
        "ETH": make_frame("ETH", 3200.0),
        "SOL": make_frame("SOL", 140.0),
    }


@pytest.fixture
def risk_empty() -> RiskState:
    return RiskState(
        balance_usd=1000.0,
        available_usd=1000.0,
        used_margin=0.0,
        exposure_ratio=0.0,
        open_position_count=0,
        max_positions=2,
        consecutive_losses=0,
        daily_pnl=0.0,
        daily_pnl_pct=0.0,
        cooldown_active=False,
    )


@pytest.fixture
def external_empty() -> ExternalContext:
    return ExternalContext(
        sentiment_value=50, sentiment_label="Neutral",
        news_summary="", forecasts={},
    )
