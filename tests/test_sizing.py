"""Risk-based sizing: notional derived from SL distance."""
from __future__ import annotations

from core.types import Side
from risk.sizing import size_position
from tests.conftest import make_frame


def test_sizing_respects_hard_cap(settings):
    frame = make_frame("BTC", price=100.0, atr_pct=0.01)
    sl = 98.0
    r = size_position(
        balance_usd=1000.0, frame=frame, side=Side.LONG, sl_price=sl,
        leverage=2, llm_size_fraction=1.0, quality_score=0.7, settings=settings,
    )
    hard_cap = 1000.0 * settings.risk.max_single_position * 2
    assert r.notional_usd <= hard_cap + 1e-6


def test_sizing_zero_on_tight_sl(settings):
    frame = make_frame("BTC", price=100.0, atr_pct=0.01)
    # SL so close it would blow up risk math — we still bound by min_sl_pct
    r = size_position(
        balance_usd=1000.0, frame=frame, side=Side.LONG, sl_price=99.9999,
        leverage=2, llm_size_fraction=0.2, quality_score=0.5, settings=settings,
    )
    assert r.sl_distance_pct >= settings.sl.min_sl_pct


def test_sizing_scales_with_quality(settings):
    frame = make_frame("BTC", price=100.0, atr_pct=0.01)
    low = size_position(
        balance_usd=1000.0, frame=frame, side=Side.LONG, sl_price=98.0,
        leverage=1, llm_size_fraction=1.0, quality_score=0.2, settings=settings,
    )
    hi = size_position(
        balance_usd=1000.0, frame=frame, side=Side.LONG, sl_price=98.0,
        leverage=1, llm_size_fraction=1.0, quality_score=0.9, settings=settings,
    )
    # Higher quality -> bigger risk budget (until hit by hard cap)
    assert hi.risk_budget_usd >= low.risk_budget_usd
